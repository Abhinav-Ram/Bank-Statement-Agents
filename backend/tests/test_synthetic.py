import pymupdf
import pytest

from app.schemas.taxonomy import ALL_CATEGORIES, ANOMALY_TYPES
from app.synthetic.generator import generate_statement
from app.synthetic.render import BANKS, inr, render, simulate_scan


@pytest.fixture(params=list(BANKS))
def stmt(request):
    layout = request.param
    return generate_statement(f"{layout}_t", layout, BANKS[layout], seed=123, months=2)


def test_reconciles(stmt):
    td = sum(t.debit for t in stmt.transactions)
    tc = sum(t.credit for t in stmt.transactions)
    assert stmt.opening_balance + tc - td == stmt.closing_balance


def test_running_balance(stmt):
    bal = stmt.opening_balance
    for t in stmt.transactions:
        assert (t.debit == 0) != (t.credit == 0), "exactly one of debit/credit"
        bal += t.credit - t.debit
        assert t.balance == bal


def test_dates_sorted_and_in_period(stmt):
    dates = [t.date for t in stmt.transactions]
    assert dates == sorted(dates)
    assert stmt.period_start <= dates[0] and dates[-1] <= stmt.period_end


def test_labels_valid(stmt):
    assert all(t.category in ALL_CATEGORIES for t in stmt.transactions)
    assert {a["type"] for a in stmt.anomalies} == set(ANOMALY_TYPES)
    n = len(stmt.transactions)
    assert all(0 <= i < n for a in stmt.anomalies for i in a["txn_idx"])


def test_duplicate_anomaly_is_a_real_duplicate(stmt):
    dup = next(a for a in stmt.anomalies if a["type"] == "duplicate_charge")
    a, b = (stmt.transactions[i] for i in dup["txn_idx"])
    assert a.description == b.description and a.debit == b.debit


def test_deterministic():
    a = generate_statement("x", "coral", "Coral", seed=5).to_ground_truth()
    b = generate_statement("x", "coral", "Coral", seed=5).to_ground_truth()
    assert a == b


def test_pdf_text_layer_and_pages(stmt):
    pdf = render(stmt)
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = "".join(p.get_text() for p in doc)
    assert inr(stmt.closing_balance) in text
    assert inr(stmt.opening_balance) in text
    pages = {t.page for t in stmt.transactions}
    assert pages == set(range(1, doc.page_count + 1))
    # every transaction's balance is printed on the page recorded for it
    for t in stmt.transactions[:: max(1, len(stmt.transactions) // 10)]:
        assert inr(t.balance) in doc[t.page - 1].get_text()


def test_scan_has_no_text_layer(stmt):
    scan = pymupdf.open(stream=simulate_scan(render(stmt), seed=1, dpi=72), filetype="pdf")
    assert all(not p.get_text().strip() for p in scan)


@pytest.mark.parametrize(
    "paise,expected",
    [(0, "0.00"), (1770, "17.70"), (12345678, "1,23,456.78"), (1000000000, "1,00,00,000.00")],
)
def test_inr(paise, expected):
    assert inr(paise) == expected
