"""Categorisation guardrails (offline: fake cache + fake LLM)."""

from app.agents.categorize import LLMCategories, categorize, normalise_merchant
from app.schemas.statement import Txn


def t(desc, debit=0, credit=0):
    return Txn(date="2024-01-01", description=desc, debit=debit, credit=credit, balance=0)


class Cache:
    def __init__(self, data=None):
        self.data, self.puts = dict(data or {}), []

    def get(self, merchants):
        return {m: self.data[m] for m in merchants if m in self.data}

    def put(self, entries):
        self.puts += entries
        for m, c, conf, _src in entries:
            self.data[m] = (c, conf)


class FakeLLM:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def chat_json(self, messages, schema):
        import json
        items = json.loads(messages[-1]["content"])
        self.calls.append([i["merchant"] for i in items])
        return LLMCategories(items=[{"merchant": i["merchant"],
                                     "category": self.answers[i["merchant"]], "confidence": 0.8}
                                    for i in items if i["merchant"] in self.answers])


def test_merchants_normalised_across_formats():
    assert normalise_merchant("POS AB7M AMZN MKTP IN*AB7M") == "AMZN MKTP"
    assert normalise_merchant("UPI/123456789012/ZEPTO MARKETPLACE/zepto@ybl") == "ZEPTO MARKETPLACE"
    assert normalise_merchant("NEFT Cr-210968206613-ICIC0SF0002-CLOTHING INDUSTRIES LTD--") == \
        "CLOTHING INDUSTRIES"
    assert normalise_merchant("ATW-484363-ATM CASH WDL-PV0L") == "ATM CASH"


def test_rules_need_no_llm_and_respect_direction():
    rows = [t("POS 4Z0J SWIGGY*ORDER", debit=45000), t("REFUND/AMZN MKTP IN/123", credit=9900),
            t("NEFT CR-ACME0001-ACME SOFTWARE PVT LTD-SALARY JAN 2025", credit=9500000)]
    llm = FakeLLM({})
    out = categorize(rows, Cache().get, Cache().put, llm)
    assert [a["category"] for a in out] == ["Dining", "Refunds", "Salary"]
    assert llm.calls == []


def test_llm_called_once_per_unknown_merchant_and_cached():
    rows = [t("POS X1Y2 FURLENCO RENTALS", debit=150000), t("POS Q9W8 MYSTERY SHOP", debit=500),
            t("POS Q9W8 MYSTERY SHOP", debit=700)]
    cache = Cache()
    llm = FakeLLM({"MYSTERY SHOP": "Shopping"})
    out = categorize(rows, cache.get, cache.put, llm)
    assert llm.calls == [["MYSTERY SHOP"]]          # FURLENCO RENTALS hit the RENTALS rule
    assert out[1]["category"] == out[2]["category"] == "Shopping"
    assert out[1]["source"] == "llm" and ("MYSTERY SHOP", "Shopping", 0.8, "llm") in cache.puts
    llm2 = FakeLLM({})
    out2 = categorize(rows[1:], cache.get, cache.put, llm2)
    assert llm2.calls == [] and out2[0]["source"] == "cache"


def test_llm_category_with_wrong_direction_is_rejected():
    rows = [t("POS Q9W8 MYSTERY SHOP", debit=500)]
    out = categorize(rows, Cache().get, Cache().put, FakeLLM({"MYSTERY SHOP": "Salary"}))
    assert out[0]["category"] == "Other Expense" and out[0]["source"] == "default"


def test_no_llm_falls_back_to_other_and_zero_amount_rows_skipped():
    rows = [t("POS Q9W8 MYSTERY SHOP", debit=500),
            t("FAILED-INSUFFICIENT FUNDS-UPI/DR/123/X/Y")]
    out = categorize(rows, Cache().get, Cache().put, None)
    assert out[0]["category"] == "Other Expense"
    assert out[1]["category"] is None and out[1]["source"] == "zero_amount"


def test_cache_entry_must_fit_direction():
    rows = [t("POS Q9W8 MYSTERY SHOP", credit=500)]
    out = categorize(rows, Cache({"MYSTERY SHOP": ("Shopping", 1.0)}).get, Cache().put, None)
    assert out[0]["category"] == "Other Income"
