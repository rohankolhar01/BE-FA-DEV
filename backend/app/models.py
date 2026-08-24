from pydantic import BaseModel
from typing import Optional, List


class Transaction(BaseModel):
    id: Optional[int] = None
    date: str
    particulars: str
    vendor: Optional[str] = None
    description: Optional[str] = None  # free-text UPI note, e.g. "water", "food"
    ref_no: Optional[str] = None
    channel: Optional[str] = None  # UPI / NEFT / IMPS / ATM / CARD / CASH / OTHER
    withdrawal: float = 0.0
    deposit: float = 0.0
    balance: Optional[float] = None
    category: str = "Uncategorized"
    type: str = "debit"  # debit | credit


class CategoryTotal(BaseModel):
    category: str
    total: float
    count: int


class MonthlyTotal(BaseModel):
    month: str
    withdrawals: float
    deposits: float


class VendorTotal(BaseModel):
    vendor: str
    debit: float
    credit: float
    count: int


class Summary(BaseModel):
    total_transactions: int
    total_withdrawals: float
    total_deposits: float
    net: float
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    by_category: List[CategoryTotal]
    by_month: List[MonthlyTotal]
    top_vendors: List[CategoryTotal]
    vendor_breakdown: List[VendorTotal] = []


class Account(BaseModel):
    id: str
    label: str
    bank_name: Optional[str] = None
    account_number_masked: Optional[str] = None
    transaction_count: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    latest_balance: Optional[float] = None


class AccountDashboard(BaseModel):
    account: Account
    transactions: List[Transaction]
    summary: Summary
    warnings: List[str] = []


class Profile(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    avatar: str = ""


class UpdateAccountRequest(BaseModel):
    label: Optional[str] = None
    bank_name: Optional[str] = None


class UpdateCategoryRequest(BaseModel):
    category: str


class Budget(BaseModel):
    id: int
    month: str
    category: str
    planned_amount: float
    note: str = ""
    entry_type: str = "expense"   # 'expense' (money out) | 'income' (money in)
    actual_amount: float = 0.0


class BudgetRequest(BaseModel):
    month: str          # 'YYYY-MM'
    category: str
    planned_amount: float = 0.0
    note: str = ""
    entry_type: str = "expense"


class CategorySpend(BaseModel):
    category: str
    expense_amount: float = 0.0
    income_amount: float = 0.0


class PlanSummary(BaseModel):
    available_balance: float = 0.0


class MonthCell(BaseModel):
    month: str                  # 'YYYY-MM'
    statement_count: int = 0
    transaction_count: int = 0
    total_expense: float = 0.0
    total_income: float = 0.0


class StatementSummary(BaseModel):
    id: int
    filename: str
    month_bucket: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    uploaded_at: Optional[str] = None
    account_label: str
    bank_name: Optional[str] = None
    account_number_masked: Optional[str] = None
    transaction_count: int = 0
    total_expense: float = 0.0
    total_income: float = 0.0


class StatementDashboard(BaseModel):
    statement: StatementSummary
    account: Account
    transactions: List[Transaction]
    summary: Summary


class RenameStatementRequest(BaseModel):
    filename: str


class EMI(BaseModel):
    id: int
    name: str
    monthly_amount: float = 0.0
    due_day: int   # 1-31, repeats every month


class EMIRequest(BaseModel):
    name: str
    monthly_amount: float = 0.0
    due_day: int


class Reminder(BaseModel):
    id: int
    title: str
    due_date: str  # 'YYYY-MM-DD'
    note: str = ""
    done: bool = False


class ReminderRequest(BaseModel):
    title: str
    due_date: str
    note: str = ""


class ReminderDoneRequest(BaseModel):
    done: bool


class User(BaseModel):
    id: int
    username: str
    name: str = ""
    email: str = ""
    phone: str = ""
    avatar: str = ""


class SignupRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str
