"""Anomaly rules on seeded synthetic statements (offline, deterministic)."""

import pytest

from app.agents.explain import explain
from app.services.anomaly import detect, fee_spikes, large_debits, new_recurring
from app.synthetic.generator import generate_statement


def rows_of(stmt):
    return [{"idx": t.idx, "date": t.date, "merchant": t.merchant.upper(), "category": t.category,
             "debit": t.debit, "credit": t.credit, "description": t.description}
            for t in stmt.transactions]


@pytest.mark.parametrize("seed", [11, 22, 33, 44, 55])
@pytest.mark.parametrize("months", [2, 3])
def test_seeded_anomalies_are_found(seed, months):
    stmt = generate_statement("x", "coral", "Coral", seed=seed, months=months)
    found = detect(rows_of(stmt))
    got = {(a["type"], a["idx"]) for a in found}
    for truth in stmt.anomalies:
        assert any((truth["type"], i) in got for i in truth["txn_idx"]), truth


@pytest.mark.parametrize("seed", [11, 22, 33])
def test_false_alarms_are_few(seed):
    stmt = generate_statement("x", "summit", "Summit", seed=seed, months=3)
    seeded = {i for a in stmt.anomalies for i in a["txn_idx"]}
    extra = [a for a in detect(rows_of(stmt)) if a["idx"] not in seeded]
    assert len(extra) <= 3, extra


def test_new_recurring_needs_something_to_compare():
    stmt = generate_statement("x", "coral", "Coral", seed=5, months=1)
    assert new_recurring(rows_of(stmt)) == []
    # with stored history for the account, a one-month statement can be judged
    assert new_recurring(rows_of(stmt), history_merchants={"SOMETHING ELSE"})


def test_routine_monthly_fee_is_not_a_spike():
    rows = [{"idx": i, "date": f"2025-0{m}-28", "merchant": "BANK CHARGES",
             "category": "Fees/Charges", "debit": 1770, "credit": 0, "description": "SMS"}
            for i, m in enumerate([1, 2, 3])]
    assert fee_spikes(rows) == []


def test_large_debit_needs_enough_history_and_size():
    rows = [{"idx": i, "date": f"2025-01-0{i + 1}", "merchant": "M", "category": "Shopping",
             "debit": d, "credit": 0, "description": ""} for i, d in enumerate([500_00, 600_00,
                                                                               4000_00])]
    assert large_debits(rows) == []  # 6.7x the median but under ₹10,000


class FakeLLM:
    def __init__(self, text):
        self.text = text

    def chat_json(self, messages, schema):
        return schema(items=[{"id": 0, "explanation": self.text}])


def test_explanations_cannot_invent_numbers():
    stmt = generate_statement("x", "coral", "Coral", seed=11, months=2)
    rows = rows_of(stmt)
    a = [x for x in detect(rows) if x["type"] == "large_debit"][:1]
    by_idx = {r["idx"]: r for r in rows}
    amt = a[0]["details"]["amount"] / 100
    ok = explain([dict(a[0])], by_idx, FakeLLM(f"A ₹{amt:,.0f} electronics purchase, far above usual."))
    assert ok[0]["explanation_source"] == "llm"
    bad = explain([dict(a[0])], by_idx, FakeLLM("You spent ₹99,999 at Croma."))
    assert bad[0]["explanation_source"] == "template"


def test_routine_fee_by_name_is_not_part_of_a_spike_even_in_one_month():
    rows = [{"idx": i, "date": "2025-01-1" + str(i), "merchant": "BANK CHARGES",
             "category": "Fees/Charges", "debit": d, "credit": 0, "description": desc}
            for i, (d, desc) in enumerate([(590_00, "CHQ RETURN CHGS"), (590_00, "CHQ RETURN"),
                                           (250_00, "ECS RETURN"), (17_70, "SMS ALERT CHGS")])]
    assert {a["idx"] for a in fee_spikes(rows)} == {0, 1, 2}


def test_new_recurring_flags_each_new_merchant_once():
    rows = [{"idx": 0, "date": "2025-01-05", "merchant": "NETFLIX", "category": "Subscriptions",
             "debit": 649_00, "credit": 0, "description": "NETFLIX"}] + [
        {"idx": i, "date": f"2025-02-1{i}", "merchant": "ACT FIBERNET", "category": "Utilities",
         "debit": 900_00 + i, "credit": 0, "description": f"ACH D- ACT FIBERNET-{i}"}
        for i in (1, 2, 3)]
    assert [a["idx"] for a in new_recurring(rows)] == [1]
