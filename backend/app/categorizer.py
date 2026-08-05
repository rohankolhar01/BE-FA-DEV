"""
Turns a raw {date, particulars, withdrawal, deposit, balance} row into a
fully-typed Transaction: extracts the payment channel (UPI/NEFT/ATM/...),
the vendor name, and assigns a spend category.

Categorization is rule-based (fast, free, works offline) with an optional
local-LLM fallback via Ollama for particulars that don't match any keyword
rule. Ollama is entirely optional - if it isn't running locally, the
categorizer just leaves those rows as "Uncategorized".
"""
import re
import os
import requests
from typing import Optional, List
from .models import Transaction

CHANNEL_RE = re.compile(r"\b(UPI|NEFT|IMPS|RTGS|ATM|POS|CARD|ECS|NACH)\b", re.IGNORECASE)
REF_RE = re.compile(r"\b\d{6,18}\b")
# Bank transaction hash, e.g. AXIDDD3A6FDAF0D426DB728F221A6DDF649
TXN_HASH_RE = re.compile(r"[A-Z]{2,4}[0-9A-F]{12,}", re.IGNORECASE)
# Trailing timestamp fragments left by the narration's date/time tail
DATETIME_RE = re.compile(r"\d{2,4}[\s:0-9]*")
# Leading channel token of a narration, e.g. "INET-IMPS-CR" - never the vendor
CHANNEL_PREFIX_RE = re.compile(
    r"^(INET|UPI|NEFT|IMPS|RTGS|MMT|ACH|NACH|POS|ATM|ECS)[-/]", re.IGNORECASE
)

# category -> keywords matched against the particulars/vendor text (lowercase)
CATEGORY_RULES = {
    "Groceries": ["bigbasket", "blinkit", "zepto", "grofers", "dmart", "grocery", "reliance fresh", "more supermarket"],
    "Food & Dining": [
        "swiggy", "zomato", "restaurant", "cafe", "food", "lunch", "dinner", "breakfast",
        "eatery", "dominos", "mcdonald", "starbucks", "kfc", "hotel", "mess", "canteen",
        "tiffin", "meals", "snacks", "juice", "chai", "tea", "coffee",
        # common Indian dishes that show up as UPI notes
        "idli", "dosa", "vada", "upma", "poha", "chitranna", "chitrana", "biryani",
        "roti", "chapati", "paratha", "paneer", "samosa", "pav bhaji", "thali", "curd",
    ],
    "Transport": ["uber", "ola", "rapido", "irctc", "indigo", "airlines", "petrol", "fuel", "metro", "fastag", "parking"],
    "Utilities": ["electricity", "bescom", "water bill", "water", "recharge", "airtel", "jio", "vodafone", "broadband", "gas bill", "postpaid"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "meesho", "shopping", "mall", "nykaa"],
    "Entertainment": ["netflix", "spotify", "hotstar", "prime video", "bookmyshow", "pvr", "inox", "youtube premium"],
    "Healthcare": ["pharmacy", "hospital", "clinic", "apollo", "medplus", "diagnostic", "medical", "doctor"],
    "Rent & Housing": ["rent", "landlord", "maintenance charge", "society"],
    "Investments": ["mutual fund", "zerodha", "groww", "upstox", "sip", "nps", "stocks"],
    "ATM / Cash": ["atm", "cash withdrawal"],
    "Salary / Income": ["salary", "payroll", "stipend"],
    "Transfers": ["neft", "imps", "rtgs", "transfer to", "transfer from", "sent to", "received from"],
    "Insurance": ["insurance", "premium", "lic"],
    "Education": ["school", "college", "tuition", "university", "course fee", "udemy", "coursera"],
}

# Whole-word matching: without it "tea" hits "steam" and "atm" hits "batman".
_COMPILED_RULES = {
    category: [re.compile(r"\b" + re.escape(k) + r"\b", re.IGNORECASE) for k in keywords]
    for category, keywords in CATEGORY_RULES.items()
}

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral")
USE_OLLAMA_FALLBACK = os.environ.get("USE_OLLAMA_FALLBACK", "false").lower() == "true"


def extract_channel(particulars: str) -> Optional[str]:
    m = CHANNEL_RE.search(particulars)
    return m.group(1).upper() if m else None


def _clean_segment(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def _is_noise(segment: str) -> bool:
    """Bank-generated identifiers that are never a vendor or a human note."""
    if REF_RE.fullmatch(segment):
        return True
    if TXN_HASH_RE.fullmatch(segment):  # e.g. AXIDDD3A6FDAF0D426DB728F221A6DDF649
        return True
    if DATETIME_RE.fullmatch(segment):  # trailing "2026 00:51:52" fragments
        return True
    return False


def extract_vendor_and_description(particulars: str) -> "tuple[Optional[str], Optional[str]]":
    """
    UPI narration is *positional*, not free-form. Real examples:

      UPI/DR/654320337391/RAMSIYA VP/YESB/**0HT3R@PTYS/WATER//AXIDDD3A...649/26/06/2026 00:51:52
      UPI/DR/617746907323/MEESEMAD/YESB/**38373@YBL/IDLI//AXICCBB3E...900/26/06/2026 21:37:16
       0   1       2           3      4       5        6  7      8      9  10       11

    Field 3 is the payee (vendor) and field 6 is the free-text note typed in
    the UPI app at payment time ("WATER", "IDLI") - the description. Splitting
    positionally is far more reliable than guessing from the surrounding junk,
    because fields 8-11 are a transaction hash and a timestamp that look
    superficially like names.

    Non-UPI narrations (NEFT/IMPS/ATM/...) have no fixed layout, so those fall
    back to picking the most name-like segments.
    """
    parts = [p.strip() for p in re.split(r"[/|]", particulars)]

    if len(parts) >= 7 and parts[0].upper() == "UPI":
        vendor = _clean_segment(parts[3])
        note = _clean_segment(parts[6])
        if vendor and _is_noise(vendor):
            vendor = None
        if note and _is_noise(note):
            note = None
        return (vendor.title() if vendor else None, note.title() if note else None)

    candidates = []
    for p in parts:
        if not p or len(p) < 3:
            continue
        if CHANNEL_RE.fullmatch(p):
            continue
        if CHANNEL_PREFIX_RE.match(p):  # "INET-IMPS-CR", "MMT/IMPS", ...
            continue
        if "@" in p:  # UPI handle like name@okhdfc
            continue
        if re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", p):  # IFSC-like
            continue
        if _is_noise(p):
            continue
        candidates.append(p)
    if not candidates:
        return None, None

    vendor = candidates[0].title()
    description = None
    if len(candidates) > 1:
        last = candidates[-1]
        if last.title() != vendor:
            description = last.title()
    return vendor, description


def rule_based_category(particulars: str, vendor: Optional[str], description: Optional[str] = None) -> str:
    # The UPI note and vendor are the meaningful signal; the raw narration is
    # mostly bank identifiers, and matching against it produces false hits.
    text = f"{vendor or ''} {description or ''}".lower().strip()
    if not text:
        text = particulars.lower()
    for category, patterns in _COMPILED_RULES.items():
        if any(p.search(text) for p in patterns):
            return category
    return "Uncategorized"


def llm_category(particulars: str) -> Optional[str]:
    """Optional local-LLM categorization fallback via Ollama. Silent no-op if unavailable."""
    if not USE_OLLAMA_FALLBACK:
        return None
    valid = list(CATEGORY_RULES.keys()) + ["Other"]
    prompt = (
        "Classify this bank transaction description into exactly one category from this list: "
        f"{', '.join(valid)}.\nDescription: {particulars}\nReply with only the category name."
    )
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=8,
        )
        resp.raise_for_status()
        answer = resp.json().get("response", "").strip()
        for v in valid:
            if v.lower() in answer.lower():
                return v
    except requests.RequestException:
        return None
    return None


def categorize_row(row: dict) -> Transaction:
    particulars = row.get("particulars", "")
    channel = extract_channel(particulars)
    vendor, description = extract_vendor_and_description(particulars)
    m = REF_RE.search(particulars)
    ref_no = m.group(0) if m else None

    category = rule_based_category(particulars, vendor, description)
    if category == "Uncategorized":
        fallback = llm_category(particulars)
        if fallback:
            category = fallback

    withdrawal = row.get("withdrawal") or 0.0
    deposit = row.get("deposit") or 0.0
    tx_type = "credit" if deposit > 0 and withdrawal == 0 else "debit"
    if tx_type == "credit" and category == "Uncategorized":
        category = "Income / Other Credit"

    return Transaction(
        date=row.get("date", ""),
        particulars=particulars,
        vendor=vendor,
        description=description,
        ref_no=ref_no,
        channel=channel or "OTHER",
        withdrawal=withdrawal,
        deposit=deposit,
        balance=row.get("balance"),
        category=category,
        type=tx_type,
    )


def categorize_rows(rows: List[dict]) -> List[Transaction]:
    return [categorize_row(r) for r in rows]
