from app.schemas.statement import StatementMeta, Txn
from app.services.validate import validate


def t(debit=0, credit=0, balance=0, ambiguous=None, d="2024-01-01"):
    return Txn(date=d, description="x", debit=debit, credit=credit, balance=balance,
               ambiguous=ambiguous or [])


META = StatementMeta(opening_balance=10000, closing_balance=10500)


def test_clean_statement_passes():
    rows = [t(credit=1000, balance=11000), t(debit=500, balance=10500)]
    r = validate(META, rows)
    assert r.ok and r.totals_ok and r.opening_ok and not r.breaks


def test_totals_mismatch_detected():
    rows = [t(credit=1000, balance=11000), t(debit=400, balance=10600)]
    r = validate(META, rows)
    assert not r.ok and not r.totals_ok


def test_running_balance_break_on_clean_row_is_source_inconsistency():
    rows = [t(credit=1000, balance=11000), t(debit=500, balance=9999), t(debit=0, credit=501,
                                                                           balance=10500)]
    r = validate(StatementMeta(opening_balance=10000, closing_balance=10500), rows)
    # row 2 follows from row 1's *printed* balance, so only row 1 breaks
    assert [b.idx for b in r.breaks] == [1]
    assert not r.totals_ok
    assert all(b.kind == "source_inconsistency" for b in r.breaks)
    assert r.retry_idx == []


def test_break_on_ambiguous_row_is_retryable():
    rows = [t(credit=1000, balance=11000), t(debit=0, credit=0, balance=10500,
                                             ambiguous=["no_amount"])]
    r = validate(META, rows)
    assert r.retry_idx == [1]


def test_swapped_debit_credit_is_fixed_deterministically():
    rows = [t(credit=1000, balance=11000), t(credit=500, balance=10500)]  # should be a debit
    r = validate(META, rows)
    assert r.ok
    assert rows[1].debit == 500 and rows[1].credit == 0
    assert r.fixes == [(1, "swapped_debit_credit")]


def test_opening_derived_from_first_row():
    rows = [t(debit=200, balance=9800), t(credit=700, balance=10500)]
    r = validate(StatementMeta(opening_balance=10000, closing_balance=10500), rows)
    assert r.opening_ok and r.ok


def test_missing_meta_uses_rows_only():
    rows = [t(credit=1000, balance=11000), t(debit=500, balance=10500)]
    r = validate(StatementMeta(), rows)
    assert r.totals_ok is None and r.opening_ok is None and r.ok


def test_empty_rows_not_ok():
    assert not validate(META, []).ok
