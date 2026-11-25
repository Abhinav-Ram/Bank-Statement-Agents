"""Summary guardrails: facts computed in code, narrative numbers checked against them."""

from app.agents.summarize import check, fact_sheet, narrate, template_narrative

ROWS = [
    {"date": "2025-01-01", "merchant": "ACME", "category": "Salary", "debit": 0, "credit": 9500000},
    {"date": "2025-01-05", "merchant": "PRESTIGE", "category": "Housing", "debit": 2200000,
     "credit": 0},
    {"date": "2025-01-09", "merchant": "SWIGGY", "category": "Dining", "debit": 45050, "credit": 0},
    {"date": "2025-02-05", "merchant": "PRESTIGE", "category": "Housing", "debit": 2200000,
     "credit": 0},
    {"date": "2025-02-12", "merchant": "CROMA", "category": "Shopping", "debit": 3000000,
     "credit": 0},
    {"date": "2025-02-14", "merchant": "FAILED", "category": None, "debit": 0, "credit": 0},
]
META = {"period_start": "2025-01-01", "period_end": "2025-02-28", "opening_balance": 1000000,
        "closing_balance": 1854950}


def test_fact_sheet_numbers_are_exact():
    fs = fact_sheet(ROWS, [], META)
    assert fs["totals"] == {"income": 95000.0, "expense": 74450.5, "net": 20549.5,
                            "transactions": 5}
    assert fs["by_category"][0] == {"category": "Salary", "head": "Income", "total": 95000.0,
                                    "count": 1, "share_pct": 100.0}
    jan, feb = fs["monthly"]
    assert jan["expense"] == 22450.5 and feb["expense"] == 52000.0
    assert feb["expense_change_pct"] == round((5200000 - 2245050) / 2245050 * 100, 1)
    assert fs["top_merchants"][0] == {"merchant": "PRESTIGE", "total": 44000.0, "count": 2}


def test_checker_accepts_fact_numbers_and_rejects_invented_ones():
    fs = fact_sheet(ROWS, [], META)
    assert check("Income was ₹95,000.00 and spending ₹74,450.50 in Jan 2025.", fs) == []
    assert check("Spending rose 131.6% in February.", fs) == []
    assert check("You saved about ₹21,000 this period.", fs) == ["21000"]
    assert check("Rent was 59% of spending.", fs) == ["59"]


def test_template_passes_its_own_check():
    fs = fact_sheet(ROWS, [{"type": "large_debit", "amount": 3000000}], META)
    assert check(template_narrative(fs), fs) == []


class FakeLLM:
    def __init__(self, texts):
        self.texts, self.calls = list(texts), 0

    def chat(self, messages, **kw):
        self.calls += 1
        return self.texts.pop(0)


def test_one_regeneration_then_template():
    fs = fact_sheet(ROWS, [], META)
    good = "Income was ₹95,000.00; rent cost ₹44,000.00 over two months."
    text, src = narrate(fs, FakeLLM(["You saved ₹21,000.", good]))
    assert (text, src) == (good, "llm_regenerated")
    llm = FakeLLM(["You saved ₹21,000.", "Still ₹21,000 saved."])
    text, src = narrate(fs, llm)
    assert src == "template" and llm.calls == 2 and check(text, fs) == []
