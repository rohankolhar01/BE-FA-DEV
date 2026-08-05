from collections import defaultdict
from typing import List
from .models import Transaction, Summary, CategoryTotal, MonthlyTotal, VendorTotal


def _month_key(date_str: str) -> str:
    """Best-effort YYYY-MM extraction from a variety of date formats."""
    import re
    m = re.match(r"(\d{2})[/-](\d{2})[/-](\d{2,4})", date_str)
    if m:
        d, mth, y = m.groups()
        if len(y) == 2:
            y = "20" + y
        return f"{y}-{mth}"
    m = re.match(r"(\d{4})[/-](\d{2})[/-](\d{2})", date_str)
    if m:
        y, mth, _ = m.groups()
        return f"{y}-{mth}"
    m = re.match(r"\d{2}\s([A-Za-z]{3})\s(\d{4})", date_str)
    if m:
        mon_map = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
                   "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
        mon, y = m.groups()
        return f"{y}-{mon_map.get(mon[:3], '01')}"
    return "Unknown"


def build_summary(transactions: List[Transaction]) -> Summary:
    total_withdrawals = sum(t.withdrawal for t in transactions)
    total_deposits = sum(t.deposit for t in transactions)

    by_category = defaultdict(lambda: {"total": 0.0, "count": 0})
    for t in transactions:
        if t.withdrawal > 0:
            by_category[t.category]["total"] += t.withdrawal
            by_category[t.category]["count"] += 1
    category_totals = [
        CategoryTotal(category=c, total=round(v["total"], 2), count=v["count"])
        for c, v in sorted(by_category.items(), key=lambda kv: kv[1]["total"], reverse=True)
    ]

    by_month = defaultdict(lambda: {"withdrawals": 0.0, "deposits": 0.0})
    for t in transactions:
        key = _month_key(t.date)
        by_month[key]["withdrawals"] += t.withdrawal
        by_month[key]["deposits"] += t.deposit
    monthly_totals = [
        MonthlyTotal(month=m, withdrawals=round(v["withdrawals"], 2), deposits=round(v["deposits"], 2))
        for m, v in sorted(by_month.items())
    ]

    by_vendor = defaultdict(lambda: {"total": 0.0, "count": 0})
    vendor_stats = defaultdict(lambda: {"debit": 0.0, "credit": 0.0, "count": 0})
    for t in transactions:
        if not t.vendor:
            continue
        if t.withdrawal > 0:
            by_vendor[t.vendor]["total"] += t.withdrawal
            by_vendor[t.vendor]["count"] += 1
        vendor_stats[t.vendor]["debit"] += t.withdrawal
        vendor_stats[t.vendor]["credit"] += t.deposit
        vendor_stats[t.vendor]["count"] += 1
    top_vendors = [
        CategoryTotal(category=v, total=round(d["total"], 2), count=d["count"])
        for v, d in sorted(by_vendor.items(), key=lambda kv: kv[1]["total"], reverse=True)[:10]
    ]
    vendor_breakdown = [
        VendorTotal(vendor=v, debit=round(d["debit"], 2), credit=round(d["credit"], 2), count=d["count"])
        for v, d in sorted(vendor_stats.items(), key=lambda kv: kv[1]["debit"] + kv[1]["credit"], reverse=True)
    ]

    balances = [t.balance for t in transactions if t.balance is not None]

    return Summary(
        total_transactions=len(transactions),
        total_withdrawals=round(total_withdrawals, 2),
        total_deposits=round(total_deposits, 2),
        net=round(total_deposits - total_withdrawals, 2),
        opening_balance=balances[0] if balances else None,
        closing_balance=balances[-1] if balances else None,
        by_category=category_totals,
        by_month=monthly_totals,
        top_vendors=top_vendors,
        vendor_breakdown=vendor_breakdown,
    )
