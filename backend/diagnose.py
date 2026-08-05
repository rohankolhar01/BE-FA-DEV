"""
Prints exactly what the parser sees in a statement PDF, so parsing problems can
be diagnosed without sending the file anywhere.

    python diagnose.py "C:\\path\\to\\statement.pdf"

Amounts and narration are shown so you can check the column mapping; redact
anything you don't want to share before pasting the output.
"""
import sys

import pdfplumber

from app.pdf_parser import extract_account_info, parse_statement
from app.categorizer import categorize_rows


def main(path: str):
    with pdfplumber.open(path) as pdf:
        print(f"pages: {len(pdf.pages)}")
        print(f"detected account info: {extract_account_info(pdf)}\n")

        print("=" * 70)
        print("TABLE HEADERS FOUND (column index -> header text)")
        print("=" * 70)
        for pno, page in enumerate(pdf.pages[:3], start=1):
            for tno, table in enumerate(page.extract_tables(), start=1):
                if not table:
                    continue
                header = [(c or "").strip().replace("\n", " ") for c in table[0]]
                print(f"page {pno} table {tno}: {list(enumerate(header))}")
                widths = {len(r) for r in table if r is not None}
                print(f"   row cell-counts seen: {sorted(widths)}")
        print()

    parsed = parse_statement(path)
    print("=" * 70)
    print(f"ROWS EXTRACTED: {len(parsed['rows'])}")
    print(f"WARNINGS: {parsed['warnings']}")
    print("=" * 70)

    for row in parsed["rows"][:6]:
        print(f"date={row['date']!r}")
        print(f"  particulars={row['particulars']!r}")
        print(f"  withdrawal={row['withdrawal']} deposit={row['deposit']} balance={row['balance']}")

    print()
    print("=" * 70)
    print("PARSED TRANSACTIONS (first 12)")
    print("=" * 70)
    print(f"{'VENDOR':<22} {'DESCRIPTION':<16} {'CATEGORY':<18} {'W':>10} {'D':>10}")
    print("-" * 80)
    for t in categorize_rows(parsed["rows"])[:12]:
        print(f"{str(t.vendor):<22} {str(t.description):<16} {t.category:<18} {t.withdrawal:>10} {t.deposit:>10}")

    txs = categorize_rows(parsed["rows"])
    print(f"\ntotal withdrawals: {sum(t.withdrawal for t in txs)}")
    print(f"total deposits:    {sum(t.deposit for t in txs)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
