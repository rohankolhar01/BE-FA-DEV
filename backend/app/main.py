import hashlib
import os
import shutil
import tempfile
from collections import Counter
from typing import List, Optional

from fastapi import Depends, FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import auth, db
from .config import COOKIE_NAME, COOKIE_SAMESITE, COOKIE_SECURE, CORS_ORIGINS, JWT_EXPIRE_MINUTES
from .pdf_parser import parse_statement
from .categorizer import categorize_rows
from .analytics import build_summary
from .exporter import export_csv, export_excel
from .categorizer import CATEGORY_RULES
from .dateutil import normalize_date
from .models import (
    Account, AccountDashboard, Budget, BudgetRequest, CategorySpend, LoginRequest,
    MonthCell, PlanSummary, Profile, RenameStatementRequest, SignupRequest, StatementDashboard,
    StatementSummary, Transaction, UpdateAccountRequest, UpdateCategoryRequest, User,
)

# Offered in the UI's category dropdown, plus the buckets the parser can assign.
ALL_CATEGORIES = sorted(
    set(CATEGORY_RULES.keys()) | {"Income / Other Credit", "Uncategorized", "Other"}
)

app = FastAPI(title="Ledger API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EXPORT_DIR = os.path.join(tempfile.gettempdir(), "finance-agent-exports")
os.makedirs(EXPORT_DIR, exist_ok=True)


@app.on_event("startup")
def on_startup():
    db.init_db()


@app.get("/api/health")
def health():
    return {"status": "ok"}


def _set_auth_cookie(response: Response, user_id: int):
    token = auth.create_access_token(user_id)
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE, max_age=JWT_EXPIRE_MINUTES * 60,
    )


# ---- auth ----

@app.post("/api/auth/signup", response_model=User)
def signup(body: SignupRequest, response: Response):
    username = body.username.strip()
    if len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters.")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")
    if db.get_user_by_username(username):
        raise HTTPException(409, "That username is already taken.")
    user = db.create_user(username, auth.hash_password(body.password))
    _set_auth_cookie(response, user["id"])
    return User(**user)


@app.post("/api/auth/login", response_model=User)
def login(body: LoginRequest, response: Response):
    user = db.get_user_by_username(body.username.strip())
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Incorrect username or password.")
    _set_auth_cookie(response, user["id"])
    return User(**user)


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"status": "logged_out"}


@app.get("/api/auth/me", response_model=User)
def me(current_user: dict = Depends(auth.get_current_user)):
    return User(**current_user)


# ---- profile ----

@app.get("/api/profile", response_model=Profile)
def get_profile(current_user: dict = Depends(auth.get_current_user)):
    return Profile(**current_user)


@app.put("/api/profile", response_model=Profile)
def update_profile(profile: Profile, current_user: dict = Depends(auth.get_current_user)):
    updated = db.update_user_profile(current_user["id"], profile.name, profile.email, profile.phone, profile.avatar)
    return Profile(**updated)


# ---- accounts ----

def _account_to_model(row: dict) -> Account:
    return Account(
        id=row["id"],
        label=row["label"],
        bank_name=row.get("bank_name"),
        account_number_masked=row.get("account_number_masked"),
        transaction_count=row.get("transaction_count", 0),
        first_date=row.get("first_date"),
        last_date=row.get("last_date"),
        latest_balance=row.get("latest_balance"),
    )


def _row_to_transaction(r: dict) -> Transaction:
    return Transaction(
        id=r["id"], date=r["date"], particulars=r["particulars"], vendor=r["vendor"],
        description=r["description"], ref_no=r["ref_no"], channel=r["channel"],
        withdrawal=r["withdrawal"] or 0.0, deposit=r["deposit"] or 0.0,
        balance=r["balance"], category=r["category"], type=r["type"],
    )


def _build_dashboard(user_id: int, account_id: str, warnings: Optional[list] = None) -> AccountDashboard:
    acc_stats = db.get_account_with_stats(user_id, account_id)
    if not acc_stats:
        raise HTTPException(404, "Account not found.")
    tx_rows = db.get_transactions(account_id)
    transactions = [_row_to_transaction(r) for r in tx_rows]
    summary = build_summary(transactions)
    account = _account_to_model(acc_stats)
    return AccountDashboard(account=account, transactions=transactions, summary=summary, warnings=warnings or [])


@app.get("/api/accounts")
def list_accounts(current_user: dict = Depends(auth.get_current_user)):
    return [_account_to_model(a) for a in db.list_accounts(current_user["id"])]


@app.get("/api/accounts/{account_id}", response_model=AccountDashboard)
def get_account_dashboard(account_id: str, current_user: dict = Depends(auth.get_current_user)):
    return _build_dashboard(current_user["id"], account_id)


@app.patch("/api/accounts/{account_id}", response_model=Account)
def update_account(account_id: str, body: UpdateAccountRequest, current_user: dict = Depends(auth.get_current_user)):
    if not db.get_account(current_user["id"], account_id):
        raise HTTPException(404, "Account not found.")
    db.update_account(current_user["id"], account_id, body.label, body.bank_name)
    return _account_to_model(db.get_account_with_stats(current_user["id"], account_id))


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: str, current_user: dict = Depends(auth.get_current_user)):
    if not db.get_account(current_user["id"], account_id):
        raise HTTPException(404, "Account not found.")
    db.delete_account(current_user["id"], account_id)
    return {"status": "deleted"}


@app.post("/api/accounts/upload", response_model=AccountDashboard)
async def upload_statement(
    file: UploadFile = File(...),
    account_id: Optional[str] = Form(None),
    current_user: dict = Depends(auth.get_current_user),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF bank statement.")

    raw = await file.read()
    file_hash = hashlib.sha256(raw).hexdigest()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        parsed = parse_statement(tmp_path)
    finally:
        os.remove(tmp_path)

    rows = parsed["rows"]
    warnings = parsed["warnings"]
    account_info = parsed["account_info"]

    if not rows:
        raise HTTPException(422, "No transactions could be extracted from this PDF.")

    transactions = categorize_rows(rows)
    tx_dicts = [t.model_dump() for t in transactions]

    user_id = current_user["id"]
    if account_id:
        if not db.get_account(user_id, account_id):
            raise HTTPException(404, "Account not found.")
        target_id = account_id
    else:
        existing = db.find_account_by_number(user_id, account_info.get("account_number_masked"))
        if existing:
            target_id = existing["id"]
        else:
            bank_name = account_info.get("bank_name")
            masked = account_info.get("account_number_masked")
            # Many statements render the bank letterhead as an image, so
            # bank_name is often None - fall back to the account number rather
            # than a meaningless "New Account", and let the user set the bank.
            if bank_name and masked:
                label = f"{bank_name} #{masked[-4:]}"
            elif bank_name:
                label = bank_name
            elif masked:
                label = f"Account #{masked[-4:]}"
            else:
                label = "New Account"
            target_id = db.create_account(user_id, label, bank_name, masked)

    inserted, refreshed, tx_ids = db.insert_transactions(target_id, tx_dicts)
    if inserted == 0 and refreshed:
        warnings.append(f"No new transactions - re-parsed {refreshed} existing entries with the current rules.")
    elif refreshed:
        warnings.append(f"Added {inserted} new entries; re-parsed {refreshed} already-imported ones.")

    # Record the document itself so Analysis can list and drill into it.
    sortable = sorted(normalize_date(r.get("date", "")) for r in rows)
    sortable = [d for d in sortable if d != "9999-99-99"]
    period_start = sortable[0] if sortable else None
    period_end = sortable[-1] if sortable else None
    # A statement can straddle two months; file it under whichever it mostly covers.
    month_bucket = None
    if sortable:
        month_bucket = Counter(d[:7] for d in sortable).most_common(1)[0][0]

    statement_id = db.upsert_statement(
        user_id, target_id, file.filename, file_hash, month_bucket, period_start, period_end,
    )
    db.link_statement_transactions(statement_id, tx_ids)

    dashboard = _build_dashboard(user_id, target_id, warnings)
    return JSONResponse(content={**dashboard.model_dump(), "statement_id": statement_id})


# ---- statements: the document browser behind the Analysis tab ----

def _statement_to_model(row: dict) -> StatementSummary:
    uploaded = row.get("uploaded_at")
    return StatementSummary(
        id=row["id"],
        filename=row["filename"],
        month_bucket=row.get("month_bucket"),
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        uploaded_at=uploaded.isoformat() if uploaded else None,
        account_label=row.get("account_label", ""),
        bank_name=row.get("bank_name"),
        account_number_masked=row.get("account_number_masked"),
        transaction_count=row.get("transaction_count", 0),
        total_expense=float(row.get("total_expense") or 0.0),
        total_income=float(row.get("total_income") or 0.0),
    )


@app.get("/api/statements/years")
def statement_years(current_user: dict = Depends(auth.get_current_user)):
    return db.statement_years(current_user["id"])


@app.get("/api/statements/months", response_model=List[MonthCell])
def statement_months(year: str, current_user: dict = Depends(auth.get_current_user)):
    rows = {r["month"]: r for r in db.statements_by_month(current_user["id"], year)}
    # Always return all twelve so the grid renders a full year, empty or not.
    return [
        MonthCell(
            month=f"{year}-{m:02d}",
            statement_count=rows.get(f"{year}-{m:02d}", {}).get("statement_count", 0),
            transaction_count=rows.get(f"{year}-{m:02d}", {}).get("transaction_count", 0),
            total_expense=float(rows.get(f"{year}-{m:02d}", {}).get("total_expense") or 0.0),
            total_income=float(rows.get(f"{year}-{m:02d}", {}).get("total_income") or 0.0),
        )
        for m in range(1, 13)
    ]


@app.get("/api/statements", response_model=List[StatementSummary])
def list_statements(month: Optional[str] = None, current_user: dict = Depends(auth.get_current_user)):
    # No month = every statement the user has ever uploaded, most recent first —
    # what the Analysis tab's upload-on-top/list-below menu shows by default.
    return [_statement_to_model(s) for s in db.list_statements(current_user["id"], month)]


@app.get("/api/statements/{statement_id}", response_model=StatementDashboard)
def get_statement_dashboard(statement_id: int, current_user: dict = Depends(auth.get_current_user)):
    stmt = db.get_statement(current_user["id"], statement_id)
    if not stmt:
        raise HTTPException(404, "Statement not found.")
    tx_rows = db.get_statement_transactions(statement_id)
    transactions = [_row_to_transaction(r) for r in tx_rows]
    summary = build_summary(transactions)
    acc_stats = db.get_account_with_stats(current_user["id"], stmt["account_id"])
    stmt["transaction_count"] = len(transactions)
    stmt["total_expense"] = sum(t.withdrawal for t in transactions)
    stmt["total_income"] = sum(t.deposit for t in transactions)
    return StatementDashboard(
        statement=_statement_to_model(stmt),
        account=_account_to_model(acc_stats),
        transactions=transactions,
        summary=summary,
    )


@app.patch("/api/statements/{statement_id}")
def rename_statement(
    statement_id: int, body: RenameStatementRequest, current_user: dict = Depends(auth.get_current_user)
):
    new_name = body.filename.strip()
    if not new_name:
        raise HTTPException(400, "Name can't be empty.")
    if not db.rename_statement(current_user["id"], statement_id, new_name):
        raise HTTPException(404, "Statement not found.")
    return {"status": "renamed"}


@app.delete("/api/statements/{statement_id}")
def remove_statement(statement_id: int, current_user: dict = Depends(auth.get_current_user)):
    if not db.delete_statement(current_user["id"], statement_id):
        raise HTTPException(404, "Statement not found.")
    return {"status": "deleted"}


# ---- categories & manual re-categorisation ----

@app.get("/api/categories")
def list_categories(current_user: dict = Depends(auth.get_current_user)):
    return ALL_CATEGORIES


@app.patch("/api/transactions/{transaction_id}", response_model=Transaction)
def update_transaction_category(
    transaction_id: int,
    body: UpdateCategoryRequest,
    current_user: dict = Depends(auth.get_current_user),
):
    row = db.set_transaction_category(current_user["id"], transaction_id, body.category)
    if not row:
        raise HTTPException(404, "Transaction not found.")
    return Transaction(
        id=row["id"], date=row["date"], particulars=row["particulars"], vendor=row["vendor"],
        description=row["description"], ref_no=row["ref_no"], channel=row["channel"],
        withdrawal=row["withdrawal"] or 0.0, deposit=row["deposit"] or 0.0,
        balance=row["balance"], category=row["category"], type=row["type"],
    )


# ---- expense planning ----

@app.get("/api/budgets", response_model=List[Budget])
def list_budgets(month: str, current_user: dict = Depends(auth.get_current_user)):
    return [Budget(**b) for b in db.list_budgets(current_user["id"], month)]


@app.get("/api/budgets/actuals", response_model=List[CategorySpend])
def budget_actuals(month: str, current_user: dict = Depends(auth.get_current_user)):
    return [CategorySpend(**r) for r in db.month_actuals(current_user["id"], month)]


@app.get("/api/budgets/summary", response_model=PlanSummary)
def plan_summary(current_user: dict = Depends(auth.get_current_user)):
    return PlanSummary(available_balance=db.available_balance(current_user["id"]))


@app.put("/api/budgets", response_model=Budget)
def save_budget(body: BudgetRequest, current_user: dict = Depends(auth.get_current_user)):
    if body.planned_amount < 0:
        raise HTTPException(400, "Planned amount cannot be negative.")
    if body.entry_type not in ("expense", "income"):
        raise HTTPException(400, "Entry type must be 'expense' or 'income'.")
    db.upsert_budget(
        current_user["id"], body.month, body.category, body.planned_amount,
        body.note, body.entry_type,
    )
    # Re-read through list_budgets so actual_amount uses the same direction logic.
    saved = next(
        (b for b in db.list_budgets(current_user["id"], body.month)
         if b["category"] == body.category and b["entry_type"] == body.entry_type),
        None,
    )
    if not saved:
        raise HTTPException(500, "Could not save the plan entry.")
    return Budget(**saved)


@app.delete("/api/budgets/{budget_id}")
def remove_budget(budget_id: int, current_user: dict = Depends(auth.get_current_user)):
    if not db.delete_budget(current_user["id"], budget_id):
        raise HTTPException(404, "Budget not found.")
    return {"status": "deleted"}


@app.get("/api/export/csv/{account_id}")
def download_csv(account_id: str, current_user: dict = Depends(auth.get_current_user)):
    dashboard = _build_dashboard(current_user["id"], account_id)
    out_path = os.path.join(EXPORT_DIR, f"{account_id}.csv")
    export_csv(dashboard.transactions, out_path)
    return FileResponse(out_path, filename="transactions.csv", media_type="text/csv")


@app.get("/api/export/excel/{account_id}")
def download_excel(account_id: str, current_user: dict = Depends(auth.get_current_user)):
    dashboard = _build_dashboard(current_user["id"], account_id)
    out_path = os.path.join(EXPORT_DIR, f"{account_id}.xlsx")
    export_excel(dashboard.transactions, dashboard.summary, out_path)
    return FileResponse(
        out_path,
        filename="transactions.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
