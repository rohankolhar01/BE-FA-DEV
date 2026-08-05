"""
Extracts raw transaction rows (date, particulars, withdrawal, deposit, balance)
from a bank statement PDF.

Two strategies are tried, in order:
  1. Table extraction via pdfplumber (works when the bank exports real table
     grid lines - SBI, HDFC, ICICI statements usually do).
  2. Line-by-line regex extraction as a fallback for statements that are
     exported as loosely formatted text instead of grid tables.

Because every bank formats statements differently, this parser is deliberately
forgiving: it tries to find a column that "looks like" a date, and columns
that "look like" amounts, rather than hard-coding exact column positions.
"""
import re
import pdfplumber
from typing import List, Dict, Optional

DATE_PATTERNS = [
    r"\d{2}[/-]\d{2}[/-]\d{2,4}",   # 01/02/2024, 01-02-24
    r"\d{2}\s[A-Za-z]{3}\s\d{4}",   # 01 Feb 2024
    r"\d{4}[/-]\d{2}[/-]\d{2}",     # 2024-02-01
]
DATE_RE = re.compile("|".join(DATE_PATTERNS))
AMOUNT_RE = re.compile(r"^-?\d[\d,]*\.?\d{0,2}$")

KNOWN_BANKS = [
    "State Bank Of India", "SBI", "HDFC Bank", "HDFC", "ICICI Bank", "ICICI",
    "Axis Bank", "Kotak Mahindra Bank", "Kotak", "Punjab National Bank", "PNB",
    "Bank Of Baroda", "Canara Bank", "Canara", "Union Bank Of India", "IDFC First Bank",
    "IDFC", "Yes Bank", "IndusInd Bank", "Federal Bank", "Bank Of India",
    "Indian Bank", "Central Bank Of India", "UCO Bank", "IDBI Bank",
    "Standard Chartered", "Citibank", "RBL Bank", "AU Small Finance Bank",
    "Bank Of Maharashtra", "Karnataka Bank", "South Indian Bank",
    "Punjab & Sind Bank", "Karur Vysya Bank", "Bandhan Bank", "Jammu & Kashmir Bank",
]
ACCOUNT_NO_RE = re.compile(
    r"(?:a/?c\s*(?:no)?|account\s*(?:no|number)?)\.?\s*[:\-]?\s*([xX*\d]{6,20})",
    re.IGNORECASE,
)


CHQ_RE = re.compile(r"\n\s*Chq\s*:?\s*\d+\s*", re.IGNORECASE)
CHQ_LINE_RE = re.compile(r"^\s*Chq\s*:?\s*\d+\s*$", re.IGNORECASE)
OPENING_BALANCE_RE = re.compile(r"\bopening\s+balance\b", re.IGNORECASE)
# A transaction narration begins with its payment channel. Used to tell where
# one entry ends and the next begins when the PDF gives us one row per line.
NARRATION_START_RE = re.compile(
    r"^\s*(UPI|NEFT|IMPS|RTGS|INET|ATM|POS|MMT|ACH|NACH|ECS|EMI|CHQ|CASH|TRF|SI|BY|TO)\b",
    re.IGNORECASE,
)


def _normalize_particulars(raw: Optional[str]) -> str:
    """
    Collapse a wrapped narration cell back into one line.

    PDF cells hard-wrap at the column edge, not at word boundaries, so a UPI
    narration comes back looking like:

        UPI/DR/654320337391/RAMSIY
        A
        VP/YESB/**0HT3R@PTYS/WAT
        ER//AXIDDD3A6FDAF0D426DB...

    Joining those lines with a space would corrupt the fields ("WAT ER"), so
    slash-delimited narrations are joined with nothing at all. Free-text
    narrations (no slashes) keep the space, since there the breaks really are
    between words.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    text = CHQ_RE.sub("\n", text)  # the bank repeats "Chq: 654320337391" in the same cell
    if "/" in text:
        text = re.sub(r"\s*\n\s*", "", text)
    else:
        text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def extract_account_info(pdf) -> Dict[str, Optional[str]]:
    """Best-effort bank name + masked account number from the first couple of pages."""
    text = ""
    for page in pdf.pages[:2]:
        text += (page.extract_text() or "") + "\n"

    # Only look ABOVE the transaction table. Bank names inside the table belong
    # to counterparties, not the issuer - a Canara statement legitimately
    # contains "AXIS BANK" in an IMPS narration, and matching it would label the
    # whole account wrong. Many banks render their letterhead as an image, so
    # detection failing here is expected; the user sets the name manually.
    table_start = re.search(r"\b(particulars|narration|description)\b", text, re.IGNORECASE)
    header_text = text[: table_start.start()] if table_start else text

    # Earliest match wins (the letterhead), ties go to the longer name so
    # "HDFC Bank" beats "HDFC" at the same position.
    bank_name = None
    best_pos = None
    for bank in KNOWN_BANKS:
        m = re.search(r"\b" + re.escape(bank) + r"\b", header_text, re.IGNORECASE)
        if not m:
            continue
        if best_pos is None or m.start() < best_pos or (m.start() == best_pos and len(bank) > len(bank_name)):
            best_pos, bank_name = m.start(), bank

    account_number_masked = None
    m = ACCOUNT_NO_RE.search(text)
    if m:
        digits = m.group(1)
        clean = re.sub(r"[xX*]", "", digits)
        if len(clean) >= 4:
            account_number_masked = f"XXXX{clean[-4:]}"
        elif len(digits) >= 4:
            account_number_masked = digits[-8:]

    return {"bank_name": bank_name, "account_number_masked": account_number_masked}


def _clean_amount(raw: Optional[str]) -> float:
    if not raw:
        return 0.0
    raw = raw.replace(",", "").replace("₹", "").strip()
    raw = raw.replace("Dr", "").replace("Cr", "").strip()
    if raw in ("", "-", "--"):
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _looks_like_amount(cell: str) -> bool:
    if not cell:
        return False
    cell = cell.replace(",", "").replace("₹", "").strip()
    cell = cell.replace("Dr", "").replace("Cr", "").strip()
    return bool(AMOUNT_RE.match(cell))


def _extract_via_tables(pdf) -> List[Dict]:
    rows: List[Dict] = []
    opening_balance: Optional[float] = None
    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if not table or len(table) < 2:
                continue
            header = [ (c or "").strip().lower() for c in table[0] ]
            date_idx = next((i for i, h in enumerate(header) if "date" in h), None)
            particulars_idx = next(
                (i for i, h in enumerate(header)
                 if any(k in h for k in ["particular", "narration", "description", "remarks"])),
                None,
            )
            withdrawal_idx = next(
                (i for i, h in enumerate(header) if any(k in h for k in ["withdrawal", "debit"])),
                None,
            )
            deposit_idx = next(
                (i for i, h in enumerate(header) if any(k in h for k in ["deposit", "credit"])),
                None,
            )
            balance_idx = next((i for i, h in enumerate(header) if "balance" in h), None)

            if date_idx is None or particulars_idx is None:
                continue  # not a transaction table

            # Statements without ruled row separators come back from pdfplumber
            # as ONE ROW PER VISUAL LINE, so a five-line UPI narration arrives as
            # five rows and only the one vertically level with the date carries
            # the date and amounts. Keeping just that row (the old behaviour)
            # threw away four fifths of the narration. Instead, accumulate lines
            # into a block and close it when the next transaction begins.
            def cell(line, idx):
                return (line[idx] or "").strip() if idx is not None and idx < len(line) else ""

            block = None
            for line in table[1:]:
                if line is None or len(line) <= date_idx:
                    continue

                date_cell = cell(line, date_idx)
                text = cell(line, particulars_idx)
                has_date = bool(DATE_RE.search(date_cell))

                if CHQ_LINE_RE.match(text):
                    text = ""  # the bank repeats "Chq: 6174..." under each entry
                if OPENING_BALANCE_RE.search(text) or OPENING_BALANCE_RE.search(" ".join(c or "" for c in line)):
                    bal = _clean_amount(cell(line, balance_idx))
                    if bal:
                        opening_balance = bal
                    continue

                # A block ends when a new narration starts, or when we meet a
                # second date (for statements that really are one row each).
                starts_new = bool(text and NARRATION_START_RE.match(text)) or (
                    has_date and block is not None and block["date"]
                )
                if block is not None and not block["lines"]:
                    starts_new = False  # narration hasn't arrived for this entry yet
                if block is None or starts_new:
                    if block is not None and block["date"]:
                        rows.append(_finish_block(block))
                    block = {"date": "", "lines": [], "withdrawal": 0.0, "deposit": 0.0, "balance": None}

                if text:
                    block["lines"].append(text)
                if has_date and not block["date"]:
                    block["date"] = date_cell
                w = _clean_amount(cell(line, withdrawal_idx))
                d = _clean_amount(cell(line, deposit_idx))
                b = cell(line, balance_idx)
                if w:
                    block["withdrawal"] = w
                if d:
                    block["deposit"] = d
                if b and _looks_like_amount(b):
                    block["balance"] = _clean_amount(b)

            if block is not None and block["date"]:
                rows.append(_finish_block(block))

    return _correct_amount_direction(rows, opening_balance)


def _finish_block(block: Dict) -> Dict:
    return {
        "date": block["date"],
        "particulars": _normalize_particulars("\n".join(block["lines"])),
        "withdrawal": block["withdrawal"],
        "deposit": block["deposit"],
        "balance": block["balance"],
    }


def _correct_amount_direction(rows: List[Dict], opening_balance: Optional[float]) -> List[Dict]:
    """
    Use the running balance to decide deposit vs withdrawal.

    Column order differs between banks (Canara prints Deposits before
    Withdrawals, most others the reverse) and blank cells can shift the
    mapping, so the header position alone is not trustworthy. The balance
    column is unambiguous: if the balance went up the money came in. Only
    applied when the movement matches the parsed amount, so statements
    without a reliable balance column are left exactly as parsed.
    """
    prev = opening_balance
    for row in rows:
        balance = row.get("balance")
        amount = (row.get("withdrawal") or 0.0) + (row.get("deposit") or 0.0)
        if prev is not None and balance is not None and amount:
            delta = round(balance - prev, 2)
            if abs(abs(delta) - amount) < 0.01:
                if delta >= 0:
                    row["deposit"], row["withdrawal"] = amount, 0.0
                else:
                    row["withdrawal"], row["deposit"] = amount, 0.0
        if balance is not None:
            prev = balance
    return rows


HEADER_FIELDS = [
    ("date", ["date"]),
    ("particulars", ["particular", "narration", "description", "remarks"]),
    ("deposit", ["deposit", "credit"]),
    ("withdrawal", ["withdrawal", "debit"]),
    ("balance", ["balance"]),
]


def _group_into_lines(words, tolerance: float = 2.5) -> List[List[dict]]:
    """Cluster words into visual lines by their vertical position."""
    lines: List[List[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _column_gutters(words, min_gap: float = 6.0) -> List[float]:
    """
    Find the vertical whitespace channels separating columns.

    Header words can't be used to infer column edges: "Particulars" is a short
    word centred over a wide column, so the midpoint between headers falls well
    inside the narration text and clips it. The gutters - x ranges where no word
    ever appears - give the true boundaries.
    """
    spans = sorted(([w["x0"], w["x1"]] for w in words), key=lambda s: s[0])
    merged: List[List[float]] = []
    for x0, x1 in spans:
        if merged and x0 <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    return [
        (merged[i][1] + merged[i + 1][0]) / 2
        for i in range(len(merged) - 1)
        if merged[i + 1][0] - merged[i][1] >= min_gap
    ]


def _find_columns(lines) -> "Optional[tuple[int, dict]]":
    """
    Locate the transaction-table header row and derive column x-boundaries.

    Returns (header_line_index, {field: (x_start, x_end)}), or None if this page
    has no header (continuation pages reuse the previous page's columns).
    """
    for idx, line in enumerate(lines):
        found = {}
        for word in line:
            text = word["text"].strip().lower()
            for field, keys in HEADER_FIELDS:
                if field in found:
                    continue
                if any(k in text for k in keys):
                    found[field] = word
                    break
        if "date" not in found or "particulars" not in found:
            continue

        # Gutters come from the transaction rows, not the page furniture above
        # them (a full-width title line would bridge every column).
        body_words = [w for body_line in lines[idx + 1:] for w in body_line]
        boundaries = _column_gutters(body_words) if body_words else []

        ranges, prev = [], float("-inf")
        for boundary in boundaries:
            ranges.append((prev, boundary))
            prev = boundary
        ranges.append((prev, float("inf")))

        bounds = {}
        for field, word in found.items():
            center = (word["x0"] + word["x1"]) / 2
            for start, end in ranges:
                if start <= center < end:
                    bounds[field] = (start, end)
                    break
        if "date" in bounds and "particulars" in bounds:
            return idx, bounds
    return None


def _cells_for_line(line, bounds) -> dict:
    """Bucket a line's words into columns by horizontal position."""
    cells = {field: [] for field in bounds}
    for word in line:
        center = (word["x0"] + word["x1"]) / 2
        for field, (start, end) in bounds.items():
            if start <= center < end:
                cells[field].append(word["text"])
                break
    return {field: " ".join(parts).strip() for field, parts in cells.items()}


def _extract_via_words(pdf) -> List[Dict]:
    """
    Rebuild transactions from word coordinates rather than extract_tables().

    Bank statements often draw no line between transactions, and pdfplumber then
    reports the table unpredictably - sometimes one row per visual line, other
    times the whole body merged into a single row. Both destroy multi-line
    narrations. Working from word positions sidesteps table detection entirely:
    words are grouped into visual lines, bucketed into columns by x-position,
    and consecutive lines are stitched into one transaction until the next
    narration begins.
    """
    rows: List[Dict] = []
    opening_balance: Optional[float] = None
    bounds = None
    block = None

    for page in pdf.pages:
        words = page.extract_words()
        if not words:
            continue
        lines = _group_into_lines(words)

        header = _find_columns(lines)
        if header is not None:
            header_idx, bounds = header
            body = lines[header_idx + 1:]
        elif bounds is not None:
            body = lines  # continuation page, reuse previous column layout
        else:
            continue

        for line in body:
            cells = _cells_for_line(line, bounds)
            date_cell = cells.get("date", "")
            text = cells.get("particulars", "")
            has_date = bool(DATE_RE.search(date_cell))

            if CHQ_LINE_RE.match(text):
                text = ""
            joined = " ".join(cells.values())
            if OPENING_BALANCE_RE.search(joined):
                bal = _clean_amount(cells.get("balance", ""))
                if bal:
                    opening_balance = bal
                continue
            if not text and not has_date:
                continue  # page furniture: footers, totals, page numbers

            starts_new = bool(text and NARRATION_START_RE.match(text)) or (
                has_date and block is not None and block["date"]
            )
            # A block that has collected no narration yet cannot be complete:
            # some statements put the date and amounts on a slightly different
            # baseline than the narration, so they arrive as separate lines and
            # the narration that follows still belongs to this entry.
            if block is not None and not block["lines"]:
                starts_new = False

            if block is None or starts_new:
                if block is not None and block["date"]:
                    rows.append(_finish_block(block))
                block = {"date": "", "lines": [], "withdrawal": 0.0, "deposit": 0.0, "balance": None}

            if text:
                block["lines"].append(text)
            if has_date and not block["date"]:
                block["date"] = date_cell
            w = _clean_amount(cells.get("withdrawal", ""))
            d = _clean_amount(cells.get("deposit", ""))
            b = cells.get("balance", "")
            if w:
                block["withdrawal"] = w
            if d:
                block["deposit"] = d
            if b and _looks_like_amount(b):
                block["balance"] = _clean_amount(b)

    if block is not None and block["date"]:
        rows.append(_finish_block(block))

    return _correct_amount_direction(rows, opening_balance)


def _extract_via_text(pdf) -> List[Dict]:
    """Fallback: scan raw text lines for a leading date and trailing amounts."""
    rows: List[Dict] = []
    for page in pdf.pages:
        text = page.extract_text() or ""
        for line in text.split("\n"):
            m = DATE_RE.match(line.strip())
            if not m:
                continue
            rest = line[m.end():].strip()
            tokens = rest.split()
            amount_tokens = [t for t in tokens if _looks_like_amount(t)]
            if not amount_tokens:
                continue
            particulars = rest
            for t in amount_tokens[-3:]:
                particulars = particulars.rsplit(t, 1)[0] if t in particulars else particulars
            particulars = particulars.strip(" -|")

            withdrawal, deposit, balance = 0.0, 0.0, None
            nums = [_clean_amount(t) for t in amount_tokens[-3:]]
            if len(nums) == 3:
                withdrawal, deposit, balance = nums
            elif len(nums) == 2:
                withdrawal, balance = nums[0], nums[1]
            elif len(nums) == 1:
                balance = nums[0]

            rows.append({
                "date": m.group(0),
                "particulars": particulars,
                "withdrawal": withdrawal,
                "deposit": deposit,
                "balance": balance,
            })
    return rows


def parse_statement(file_path: str) -> Dict:
    """Returns {"rows": [...], "warnings": [...], "account_info": {...}}"""
    warnings: List[str] = []
    rows: List[Dict] = []
    with pdfplumber.open(file_path) as pdf:
        account_info = extract_account_info(pdf)
        # Word-position reconstruction first: it handles multi-line narrations
        # regardless of whether the statement rules its rows. Table and raw-text
        # extraction remain as fallbacks for layouts it can't find a header in.
        rows = _extract_via_words(pdf)
        if not rows:
            rows = _extract_via_tables(pdf)
        if not rows:
            warnings.append(
                "No grid tables detected - fell back to text-line parsing. "
                "Double check the extracted rows for accuracy."
            )
            rows = _extract_via_text(pdf)
    if not rows:
        warnings.append(
            "Could not automatically detect any transaction rows in this PDF. "
            "The statement layout may not be supported yet."
        )
    return {"rows": rows, "warnings": warnings, "account_info": account_info}
