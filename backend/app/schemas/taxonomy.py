"""Fixed category taxonomy (used as an enum in LLM schemas)."""

INCOME = ["Salary", "Business/Freelance", "Interest", "Refunds", "Transfers In", "Other Income"]
EXPENSE = [
    "Housing", "Utilities", "Groceries", "Dining", "Transport/Fuel", "Shopping", "Health",
    "Education", "EMI/Loans", "Insurance", "Subscriptions", "Cash Withdrawal", "Fees/Charges",
    "Transfers Out", "Other Expense",
]
# "Other" exists under both heads in the plan; suffixes keep names unique for enums.

HEAD_OF = {c: "Income" for c in INCOME} | {c: "Expense" for c in EXPENSE}
ALL_CATEGORIES = INCOME + EXPENSE

ANOMALY_TYPES = ["duplicate_charge", "large_debit", "new_recurring", "fee_spike"]
