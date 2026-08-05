"""
PostgreSQL persistence: users (auth + profile), their bank accounts, and
each account's transactions. All account/transaction access is scoped by
user_id so one user's statements are never visible to another.

Re-uploads are deduplicated by hashing each row's date/particulars/amounts,
enforced via a UNIQUE(account_id, row_hash) constraint + ON CONFLICT DO NOTHING.
"""
import hashlib
import uuid
from contextlib import contextmanager
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extras

from .config import DATABASE_URL
from .dateutil import normalize_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    avatar TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    bank_name TEXT,
    account_number_masked TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT,
    date_sort TEXT,
    particulars TEXT,
    vendor TEXT,
    description TEXT,
    ref_no TEXT,
    channel TEXT,
    withdrawal DOUBLE PRECISION DEFAULT 0,
    deposit DOUBLE PRECISION DEFAULT 0,
    balance DOUBLE PRECISION,
    category TEXT,
    type TEXT,
    row_hash TEXT NOT NULL,
    category_locked BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE(account_id, row_hash)
);

CREATE TABLE IF NOT EXISTS budgets (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month TEXT NOT NULL,                -- 'YYYY-MM'
    category TEXT NOT NULL,
    planned_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id, month, category)
);

-- One row per uploaded PDF, so the Analysis tab can list documents and drill
-- into each one rather than jumping straight to a merged account view.
CREATE TABLE IF NOT EXISTS statements (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    month_bucket TEXT,                  -- 'YYYY-MM' the statement mostly covers
    period_start TEXT,
    period_end TEXT,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, file_hash)
);

-- Many-to-many: overlapping statements can legitimately share transactions,
-- and each statement should still show exactly what it contained.
CREATE TABLE IF NOT EXISTS statement_transactions (
    statement_id INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    PRIMARY KEY (statement_id, transaction_id)
);

-- Added after the first release; harmless if they already exist.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_locked BOOLEAN NOT NULL DEFAULT FALSE;

-- A plan line is either money going out ('expense') or coming in ('income'),
-- so the same category can legitimately appear once for each direction.
ALTER TABLE budgets ADD COLUMN IF NOT EXISTS entry_type TEXT NOT NULL DEFAULT 'expense';
ALTER TABLE budgets DROP CONSTRAINT IF EXISTS budgets_user_id_month_category_key;
CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_entry
    ON budgets (user_id, month, category, entry_type);
"""


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)


def _row_hash(row: dict) -> str:
    """
    Identity of a statement line, used to dedupe re-uploads.

    Deliberately built from the bank's own numbers (date + amounts + running
    balance) and NOT from `particulars`: the narration is reshaped by our
    parser, so including it would mean every parser improvement produced a
    different hash and therefore duplicate rows for the same transaction.
    The running balance makes collisions essentially impossible; when a
    statement has no balance column we fall back to the narration.
    """
    key = f"{row.get('date')}|{row.get('withdrawal')}|{row.get('deposit')}|{row.get('balance')}"
    if row.get("balance") is None:
        key += f"|{row.get('particulars')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---- users ----

def create_user(username: str, password_hash: str) -> Dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING *",
                (username, password_hash),
            )
            return dict(cur.fetchone())


def get_user_by_username(username: str) -> Optional[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def update_user_profile(user_id: int, name: str, email: str, phone: str, avatar: str) -> Dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET name = %s, email = %s, phone = %s, avatar = %s WHERE id = %s RETURNING *",
                (name, email, phone, avatar, user_id),
            )
            return dict(cur.fetchone())


# ---- accounts ----

def find_account_by_number(user_id: int, account_number_masked: Optional[str]) -> Optional[Dict]:
    if not account_number_masked:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM accounts WHERE user_id = %s AND account_number_masked = %s",
                (user_id, account_number_masked),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def create_account(user_id: int, label: str, bank_name: Optional[str], account_number_masked: Optional[str]) -> str:
    account_id = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (id, user_id, label, bank_name, account_number_masked) "
                "VALUES (%s, %s, %s, %s, %s)",
                (account_id, user_id, label, bank_name, account_number_masked),
            )
    return account_id


def get_account(user_id: int, account_id: str) -> Optional[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM accounts WHERE id = %s AND user_id = %s", (account_id, user_id)
            )
            row = cur.fetchone()
            return dict(row) if row else None


def update_account(user_id: int, account_id: str, label: Optional[str] = None, bank_name: Optional[str] = None):
    """Update only the fields that were supplied; leave the rest untouched."""
    sets, params = [], []
    if label is not None:
        sets.append("label = %s")
        params.append(label)
    if bank_name is not None:
        sets.append("bank_name = %s")
        params.append(bank_name)
    if not sets:
        return
    params.extend([account_id, user_id])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE accounts SET {', '.join(sets)} WHERE id = %s AND user_id = %s",
                params,
            )


def delete_account(user_id: int, account_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE id = %s AND user_id = %s", (account_id, user_id))


def _with_stats(cur, acc: dict) -> Dict:
    cur.execute(
        "SELECT COUNT(*) AS cnt, MIN(date_sort) AS first_date, MAX(date_sort) AS last_date "
        "FROM transactions WHERE account_id = %s",
        (acc["id"],),
    )
    stats = cur.fetchone()
    cur.execute(
        "SELECT balance FROM transactions WHERE account_id = %s AND balance IS NOT NULL "
        "ORDER BY date_sort DESC, id DESC LIMIT 1",
        (acc["id"],),
    )
    last_balance_row = cur.fetchone()
    return {
        "id": acc["id"],
        "label": acc["label"],
        "bank_name": acc["bank_name"],
        "account_number_masked": acc["account_number_masked"],
        "transaction_count": stats["cnt"] or 0,
        "first_date": stats["first_date"] if stats["cnt"] else None,
        "last_date": stats["last_date"] if stats["cnt"] else None,
        "latest_balance": last_balance_row["balance"] if last_balance_row else None,
    }


def list_accounts(user_id: int) -> List[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM accounts WHERE user_id = %s ORDER BY created_at", (user_id,))
            accounts = cur.fetchall()
            return [_with_stats(cur, acc) for acc in accounts]


def get_account_with_stats(user_id: int, account_id: str) -> Optional[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM accounts WHERE id = %s AND user_id = %s", (account_id, user_id)
            )
            acc = cur.fetchone()
            if not acc:
                return None
            return _with_stats(cur, acc)


# ---- transactions ----

def insert_transactions(account_id: str, rows: List[dict]) -> "tuple[int, int, List[int]]":
    """
    Store parsed rows, returning (inserted, refreshed, transaction_ids).

    A row that already exists is UPDATED rather than skipped, so re-uploading a
    statement re-applies the current parsing (vendor, description, category) to
    transactions that were imported by an older version of the parser. The ids
    are returned so the caller can attribute them to the statement they came in.
    """
    inserted = refreshed = 0
    tx_ids: List[int] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for row in rows:
                row_hash = _row_hash(row)
                date_sort = normalize_date(row.get("date", ""))
                cur.execute(
                    """INSERT INTO transactions
                       (account_id, date, date_sort, particulars, vendor, description, ref_no,
                        channel, withdrawal, deposit, balance, category, type, row_hash)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (account_id, row_hash) DO UPDATE SET
                           particulars = EXCLUDED.particulars,
                           vendor      = EXCLUDED.vendor,
                           description = EXCLUDED.description,
                           ref_no      = EXCLUDED.ref_no,
                           channel     = EXCLUDED.channel,
                           type        = EXCLUDED.type,
                           -- never clobber a category the user set by hand
                           category    = CASE WHEN transactions.category_locked
                                              THEN transactions.category
                                              ELSE EXCLUDED.category END
                       RETURNING id, (xmax = 0) AS was_inserted""",
                    (
                        account_id, row.get("date"), date_sort, row.get("particulars"),
                        row.get("vendor"), row.get("description"), row.get("ref_no"),
                        row.get("channel"), row.get("withdrawal", 0.0), row.get("deposit", 0.0),
                        row.get("balance"), row.get("category"), row.get("type"), row_hash,
                    ),
                )
                result = cur.fetchone()
                if result:
                    tx_ids.append(result["id"])
                    if result["was_inserted"]:
                        inserted += 1
                    else:
                        refreshed += 1
    return inserted, refreshed, tx_ids


def get_transactions(account_id: str) -> List[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM transactions WHERE account_id = %s ORDER BY date_sort, id",
                (account_id,),
            )
            return [dict(r) for r in cur.fetchall()]


# ---- statements (uploaded documents) ----

def upsert_statement(
    user_id: int, account_id: str, filename: str, file_hash: str,
    month_bucket: Optional[str], period_start: Optional[str], period_end: Optional[str],
) -> int:
    """Record an uploaded PDF. Re-uploading the same file updates it in place."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO statements
                       (user_id, account_id, filename, file_hash, month_bucket, period_start, period_end)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id, file_hash) DO UPDATE SET
                       filename     = EXCLUDED.filename,
                       account_id   = EXCLUDED.account_id,
                       month_bucket = EXCLUDED.month_bucket,
                       period_start = EXCLUDED.period_start,
                       period_end   = EXCLUDED.period_end,
                       uploaded_at  = now()
                   RETURNING id""",
                (user_id, account_id, filename, file_hash, month_bucket, period_start, period_end),
            )
            return cur.fetchone()["id"]


def link_statement_transactions(statement_id: int, transaction_ids: List[int]):
    if not transaction_ids:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Re-uploading a corrected file should not keep stale links around.
            cur.execute("DELETE FROM statement_transactions WHERE statement_id = %s", (statement_id,))
            cur.executemany(
                "INSERT INTO statement_transactions (statement_id, transaction_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [(statement_id, tid) for tid in transaction_ids],
            )


def statement_years(user_id: int) -> List[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT LEFT(month_bucket, 4) AS year FROM statements "
                "WHERE user_id = %s AND month_bucket IS NOT NULL ORDER BY year DESC",
                (user_id,),
            )
            return [r["year"] for r in cur.fetchall()]


def statements_by_month(user_id: int, year: str) -> List[Dict]:
    """Per-month rollup for the month grid: how many documents and what they total."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.month_bucket AS month,
                          COUNT(DISTINCT s.id) AS statement_count,
                          COUNT(DISTINCT st.transaction_id) AS transaction_count,
                          COALESCE(SUM(t.withdrawal), 0) AS total_expense,
                          COALESCE(SUM(t.deposit), 0) AS total_income
                   FROM statements s
                   LEFT JOIN statement_transactions st ON st.statement_id = s.id
                   LEFT JOIN transactions t ON t.id = st.transaction_id
                   WHERE s.user_id = %s AND s.month_bucket LIKE %s
                   GROUP BY s.month_bucket
                   ORDER BY s.month_bucket""",
                (user_id, f"{year}%"),
            )
            return [dict(r) for r in cur.fetchall()]


def list_statements(user_id: int, month: str) -> List[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.id, s.filename, s.month_bucket, s.period_start, s.period_end,
                          s.uploaded_at, a.label AS account_label, a.bank_name,
                          a.account_number_masked,
                          COUNT(st.transaction_id) AS transaction_count,
                          COALESCE(SUM(t.withdrawal), 0) AS total_expense,
                          COALESCE(SUM(t.deposit), 0) AS total_income
                   FROM statements s
                   JOIN accounts a ON a.id = s.account_id
                   LEFT JOIN statement_transactions st ON st.statement_id = s.id
                   LEFT JOIN transactions t ON t.id = st.transaction_id
                   WHERE s.user_id = %s AND s.month_bucket = %s
                   GROUP BY s.id, a.label, a.bank_name, a.account_number_masked
                   ORDER BY s.uploaded_at DESC""",
                (user_id, month),
            )
            return [dict(r) for r in cur.fetchall()]


def get_statement(user_id: int, statement_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.*, a.label AS account_label, a.bank_name, a.account_number_masked
                   FROM statements s JOIN accounts a ON a.id = s.account_id
                   WHERE s.id = %s AND s.user_id = %s""",
                (statement_id, user_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_statement_transactions(statement_id: int) -> List[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.* FROM transactions t
                   JOIN statement_transactions st ON st.transaction_id = t.id
                   WHERE st.statement_id = %s
                   ORDER BY t.date_sort, t.id""",
                (statement_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def delete_statement(user_id: int, statement_id: int) -> bool:
    """Removes the document and any transactions no other statement still claims."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM statements WHERE id = %s AND user_id = %s", (statement_id, user_id))
            if not cur.fetchone():
                return False
            cur.execute(
                """DELETE FROM transactions WHERE id IN (
                       SELECT st.transaction_id FROM statement_transactions st
                       WHERE st.statement_id = %s
                         AND NOT EXISTS (
                             SELECT 1 FROM statement_transactions other
                             WHERE other.transaction_id = st.transaction_id
                               AND other.statement_id <> %s
                         )
                   )""",
                (statement_id, statement_id),
            )
            cur.execute("DELETE FROM statements WHERE id = %s", (statement_id,))
            return True


def set_transaction_category(user_id: int, transaction_id: int, category: str) -> Optional[Dict]:
    """Manually re-categorize a transaction; locks it against future re-parsing."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE transactions t
                   SET category = %s, category_locked = TRUE
                   FROM accounts a
                   WHERE t.account_id = a.id AND t.id = %s AND a.user_id = %s
                   RETURNING t.*""",
                (category, transaction_id, user_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


# ---- budgets ----

def list_budgets(user_id: int, month: str) -> List[Dict]:
    """
    Plan lines for a month, each with what actually happened against it.

    An 'expense' line is measured against money that left the account
    (withdrawals); an 'income' line against money that arrived (deposits).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT b.*,
                          COALESCE(CASE WHEN b.entry_type = 'income'
                                        THEN inc.total ELSE exp.total END, 0) AS actual_amount
                   FROM budgets b
                   LEFT JOIN (
                       SELECT t.category, SUM(t.withdrawal) AS total
                       FROM transactions t JOIN accounts a ON a.id = t.account_id
                       WHERE a.user_id = %s AND t.date_sort LIKE %s
                       GROUP BY t.category
                   ) exp ON exp.category = b.category
                   LEFT JOIN (
                       SELECT t.category, SUM(t.deposit) AS total
                       FROM transactions t JOIN accounts a ON a.id = t.account_id
                       WHERE a.user_id = %s AND t.date_sort LIKE %s
                       GROUP BY t.category
                   ) inc ON inc.category = b.category
                   WHERE b.user_id = %s AND b.month = %s
                   ORDER BY b.entry_type DESC, b.category""",
                (user_id, f"{month}%", user_id, f"{month}%", user_id, month),
            )
            return [dict(r) for r in cur.fetchall()]


def upsert_budget(
    user_id: int, month: str, category: str, planned_amount: float, note: str,
    entry_type: str = "expense",
) -> Dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO budgets (user_id, month, category, planned_amount, note, entry_type)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id, month, category, entry_type) DO UPDATE SET
                       planned_amount = EXCLUDED.planned_amount,
                       note = EXCLUDED.note
                   RETURNING *""",
                (user_id, month, category, planned_amount, note, entry_type),
            )
            return dict(cur.fetchone())


def available_balance(user_id: int) -> float:
    """Latest known balance summed across the user's accounts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(latest.balance), 0) AS total FROM (
                       SELECT DISTINCT ON (t.account_id) t.balance
                       FROM transactions t
                       JOIN accounts a ON a.id = t.account_id
                       WHERE a.user_id = %s AND t.balance IS NOT NULL
                       ORDER BY t.account_id, t.date_sort DESC, t.id DESC
                   ) latest""",
                (user_id,),
            )
            return float(cur.fetchone()["total"] or 0.0)


def delete_budget(user_id: int, budget_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM budgets WHERE id = %s AND user_id = %s", (budget_id, user_id))
            return cur.rowcount > 0


def month_actuals(user_id: int, month: str) -> List[Dict]:
    """Actual money out and in per category for a month, regardless of any plan."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.category,
                          COALESCE(SUM(t.withdrawal), 0) AS expense_amount,
                          COALESCE(SUM(t.deposit), 0)    AS income_amount
                   FROM transactions t
                   JOIN accounts a ON a.id = t.account_id
                   WHERE a.user_id = %s AND t.date_sort LIKE %s
                   GROUP BY t.category
                   HAVING SUM(t.withdrawal) > 0 OR SUM(t.deposit) > 0
                   ORDER BY SUM(t.withdrawal) DESC""",
                (user_id, f"{month}%"),
            )
            return [dict(r) for r in cur.fetchall()]
