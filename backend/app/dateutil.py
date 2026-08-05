"""Best-effort normalization of the many date formats bank statements use
into a sortable YYYY-MM-DD string. Unparseable dates sort last."""
import re

MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def normalize_date(date_str: str) -> str:
    if not date_str:
        return "9999-99-99"
    s = date_str.strip()

    m = re.match(r"(\d{4})[/-](\d{2})[/-](\d{2})$", s)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}"

    m = re.match(r"(\d{2})[/-](\d{2})[/-](\d{2,4})$", s)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2:
            y = "20" + y
        return f"{y}-{mo}-{d}"

    m = re.match(r"(\d{2})\s([A-Za-z]{3})\s(\d{4})$", s)
    if m:
        d, mon, y = m.groups()
        return f"{y}-{MONTHS.get(mon[:3].lower(), '99')}-{d}"

    return "9999-99-99"
