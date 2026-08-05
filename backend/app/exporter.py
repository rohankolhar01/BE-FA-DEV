import pandas as pd
from typing import List
from .models import Transaction, Summary


def _to_dataframe(transactions: List[Transaction]) -> pd.DataFrame:
    return pd.DataFrame([t.model_dump() for t in transactions])


def export_csv(transactions: List[Transaction], out_path: str) -> str:
    df = _to_dataframe(transactions)
    df.to_csv(out_path, index=False)
    return out_path


def export_excel(transactions: List[Transaction], summary: Summary, out_path: str) -> str:
    df = _to_dataframe(transactions)
    cat_df = pd.DataFrame([c.model_dump() for c in summary.by_category])
    month_df = pd.DataFrame([m.model_dump() for m in summary.by_month])
    vendor_df = pd.DataFrame([v.model_dump() for v in summary.top_vendors])

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Transactions", index=False)
        cat_df.to_excel(writer, sheet_name="By Category", index=False)
        month_df.to_excel(writer, sheet_name="By Month", index=False)
        vendor_df.to_excel(writer, sheet_name="Top Vendors", index=False)

        # Auto-width columns for readability
        for sheet_name, frame in [
            ("Transactions", df), ("By Category", cat_df),
            ("By Month", month_df), ("Top Vendors", vendor_df),
        ]:
            ws = writer.sheets[sheet_name]
            for i, col in enumerate(frame.columns, start=1):
                max_len = max([len(str(col))] + [len(str(v)) for v in frame[col].astype(str)]) if len(frame) else len(str(col))
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max_len + 2, 40)

    return out_path
