"""Ingestion graph routing with a fake LLM (no network)."""

import pytest

from app.config import get_settings
from app.datasets.truth import iter_dataset
from app.graphs import ingestion
from app.schemas.statement import LLMMeta, LLMRow, LLMRows

SYN = iter_dataset("synthetic", get_settings().data_dir)
pytestmark = pytest.mark.skipif(not SYN, reason="run scripts/generate_synthetic.py first")


class FakeLLM:
    def __init__(self, meta: LLMMeta | None = None, rows_fn=None):
        self.meta, self.rows_fn = meta, rows_fn
        self.usage = {"calls": 0, "cached_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.seen = []

    def chat_json(self, messages, schema):
        self.usage["calls"] += 1
        self.seen.append(schema.__name__)
        if schema is LLMMeta:
            return self.meta
        return self.rows_fn(messages)


def meta_for(truth) -> LLMMeta:
    m = truth.meta
    return LLMMeta(bank_name=m.bank_name, account_holder=m.account_holder,
                   account_number="XXXX" + (m.account_masked or ""), period_start=m.period_start,
                   period_end=m.period_end, opening_balance=m.opening_balance / 100,
                   closing_balance=m.closing_balance / 100, currency="INR")


def test_clean_statement_is_ok_with_one_llm_call(monkeypatch):
    truth = SYN[0]
    fake = FakeLLM(meta_for(truth))
    monkeypatch.setattr(ingestion, "_llm", lambda: fake)
    st = ingestion.run_ingestion(str(truth.pdf))
    assert st["status"] == "ok" and st["report"]["ok"]
    assert fake.seen == ["LLMMeta"]  # nothing ambiguous → no row calls
    assert len(st["rows"]) == len(truth.rows)
    assert [e["node"] for e in st["events"]] == ["parse", "extract_metadata",
                                                 "repair_ambiguous", "validate"]


def test_wrong_closing_balance_goes_to_review_without_row_retries(monkeypatch):
    truth = SYN[0]
    meta = meta_for(truth)
    meta.closing_balance += 1
    fake = FakeLLM(meta)
    monkeypatch.setattr(ingestion, "_llm", lambda: fake)
    st = ingestion.run_ingestion(str(truth.pdf))
    assert st["status"] == "awaiting_review" and st["report"]["totals_ok"] is False
    assert st["attempts"] == 0


def test_scan_routes_to_needs_ocr(monkeypatch):
    scans = iter_dataset("synthetic_scan", get_settings().data_dir)
    if not scans:
        pytest.skip("no scans")
    monkeypatch.setattr(ingestion, "_llm", lambda: pytest.fail("LLM must not be called"))
    st = ingestion.run_ingestion(str(scans[0].pdf), ocr_engine=None)
    assert st["status"] == "needs_ocr"


def test_retry_loop_is_capped(monkeypatch):
    # An ambiguous row whose LLM repair never adds up → 1 repair + MAX_RETRIES retries, then review.
    truth = SYN[0]
    real_parse = ingestion.parse_pdf

    def parse_with_bad_row(path, ocr_engine=None):
        res = real_parse(path, ocr_engine=ocr_engine)
        res.rows[3].debit = res.rows[3].credit = 0
        res.rows[3].ambiguous = ["no_amount"]
        return res

    def bad_rows(messages):
        return LLMRows(rows=[LLMRow(date="2025-01-02", description="x", debit=1.0, credit=None,
                                    balance=1.0)])

    fake = FakeLLM(meta_for(truth), bad_rows)
    monkeypatch.setattr(ingestion, "parse_pdf", parse_with_bad_row)
    monkeypatch.setattr(ingestion, "_llm", lambda: fake)
    st = ingestion.run_ingestion(str(truth.pdf))
    assert st["status"] == "awaiting_review"
    assert st["attempts"] == ingestion.MAX_RETRIES
    assert fake.seen.count("LLMRows") == 1 + ingestion.MAX_RETRIES


def test_llm_repair_cannot_invent_numbers():
    from app.agents.extract import repair_rows
    from app.schemas.statement import Txn

    row = Txn(date="2024-02-21", description="FAILED-INSUFFICIENT FUNDS-UPI/DR/8007",
              balance=177531245, ambiguous=["no_amount"],
              raw={"date": "21-02-2024", "description": "FAILED-INSUFFICIENT FUNDS-UPI/DR/8007",
                   "balance": "Rs. 1,775,312.45"})
    invented = FakeLLM(rows_fn=lambda m: LLMRows(rows=[LLMRow(
        date="2024-02-21", description="x", debit=2984.0, credit=None, balance=672278.09)]))
    out = repair_rows(invented, [row], prev_balance=None)
    assert out[0].debit == 0 and out[0].balance == 177531245
    assert "llm_unverified" in out[0].ambiguous

    honest = FakeLLM(rows_fn=lambda m: LLMRows(rows=[LLMRow(
        date="2024-02-21", description="FAILED", debit=None, credit=None, balance=1775312.45)]))
    out = repair_rows(honest, [row], prev_balance=None)
    assert out[0].source == "llm" and out[0].ambiguous == []


def test_numbers_in_dates_or_descriptions_do_not_verify_amounts():
    from app.agents.extract import printed_amounts
    from app.schemas.statement import Txn

    row = Txn(date="2024-01-22", balance=860951213, raw={
        "posted": "22/01/2024 01:45:10 PM", "description": "UPI/DR/8146/Nilesh/7424@pay",
        "drcr": "DR", "balance": "8,609,512.13"})
    assert printed_amounts(row) == {860951213}
