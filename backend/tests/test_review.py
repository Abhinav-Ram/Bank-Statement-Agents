"""Human-review payload: readable, actionable, and its suggestions really fix what they claim."""

import json

import pytest

from app.graphs.ingestion import apply_corrections
from app.schemas.statement import StatementMeta, Txn
from app.services.review import (
    ReviewAnswer,
    ReviewRequest,
    build_review,
    inr,
    parse_answer,
    to_internal,
)
from app.services.validate import validate


def T(d, debit=0, credit=0, bal=None, **kw):
    return Txn(date=f"2025-01-{d:02d}", description=f"TXN {d}", debit=debit, credit=credit,
               balance=bal, page=1, bbox=(40.0, 100.0 + 12 * d, 560.0, 110.0 + 12 * d), **kw)


META = StatementMeta(bank_name="Coral", period_start="2025-01-01", period_end="2025-01-31",
                     opening_balance=1000_00, closing_balance=300_00)
LAYOUTS = [{"page": 1, "width": 595.0, "height": 842.0}]


def review(rows, meta=META):
    rep = validate(meta, rows)
    report = {"ok": rep.ok, "opening_ok": rep.opening_ok, "totals_ok": rep.totals_ok,
              "closing_matches_last": rep.closing_matches_last,
              "breaks": [b.__dict__ for b in rep.breaks], "errors": rep.errors()}
    return build_review(stmt_id="st_x", pdf="/tmp/x.pdf", meta=meta, rows=rows, report=report,
                        layouts=LAYOUTS)


def apply(rows, meta, suggestion):
    ans = ReviewAnswer(action="correct", rows=suggestion.rows, meta=suggestion.meta)
    edits, meta_upd = to_internal(ans)
    return apply_corrections(rows, edits), meta.model_copy(update=meta_upd)


def test_inr_uses_indian_grouping():
    assert inr(12345678) == "₹1,23,456.78"
    assert inr(123456789012) == "₹1,23,45,67,890.12"
    assert inr(100) == "₹1.00" and inr(-50000) == "-₹500.00" and inr(99999) == "₹999.99"


def test_misread_amount_issue_is_readable_and_its_likely_fix_reconciles():
    rows = [T(1, debit=100_00, bal=900_00, source="ocr"), T(2, debit=50_00, bal=400_00,
                                                            source="ocr_reread"),
            T(3, debit=100_00, bal=300_00, source="ocr")]
    req = review(rows)
    iss, closing = req.issues  # the totals then can't match either: flagged as a follow-on
    assert closing.kind == "closing_balance" and closing.severity == "check"
    assert "once the rows above are fixed" in closing.explanation
    assert iss.kind == "balance_break" and iss.severity == "fix" and iss.row_idx == 1
    assert iss.title == "Row 2: the balance doesn't add up"
    assert "₹900.00" in iss.explanation and "₹400.00" in iss.explanation  # money in ₹
    assert [v.role for v in iss.context] == ["before", "flagged", "after"]
    assert iss.location.page == 1 and iss.location.page_width == 595.0
    best = iss.suggestions[0]
    assert best.likely and best.label == "Amount is ₹500.00 debit (balance is right)"
    fixed, meta = apply(rows, META, best)
    assert validate(meta, fixed).ok and fixed[1].source == "user"


def test_misread_balance_issue_suggests_balance_and_says_it_fixes_next_row():
    rows = [T(1, debit=100_00, bal=900_00, source="ocr"),
            T(2, debit=50_00, bal=8500_00, source="ocr_reread"),
            T(3, debit=550_00, bal=300_00, source="ocr")]
    req = review(rows)
    first = next(i for i in req.issues if i.row_idx == 1)
    best = first.suggestions[0]
    assert best.likely and best.label == "Balance is ₹850.00 (amount is right)"
    assert "row 3" in best.why
    knock = next(i for i in req.issues if i.row_idx == 2)
    assert knock.severity == "check" and knock.title == "Row 3: follows from row 2"
    assert knock.suggestions == []  # the fix belongs to row 2; nothing to tempt a wrong change
    fixed, meta = apply(rows, META, best)
    assert validate(meta, fixed).ok


def test_printed_inconsistency_with_no_single_fix_is_a_check():
    rows = [T(1, debit=100_00, bal=900_00), T(2, debit=50_00, bal=400_00),
            T(3, debit=100_00, bal=250_00), T(4, debit=10_00, bal=245_00)]
    # clean text layer; no single value makes the neighbours add up
    req = review(rows, META.model_copy(update={"closing_balance": 100_00}))
    assert all(i.severity == "check" for i in req.issues)
    assert "Keep as printed" in req.issues[0].explanation
    assert "Keep as printed" in req.headline


def test_printed_typo_that_one_value_fixes_is_flagged_as_likely_typo():
    rows = [T(1, debit=100_00, bal=900_00), T(2, debit=50_00, bal=400_00),
            T(3, debit=100_00, bal=300_00)]  # clean text layer; amount 500 fixes everything
    req = review(rows)
    iss = req.issues[0]
    assert iss.severity == "fix" and "typo on the statement" in iss.explanation
    assert "likely wrong" in req.headline


def test_wrong_closing_balance_gets_a_one_click_fix():
    rows = [T(1, debit=100_00, bal=900_00), T(2, debit=500_00, bal=400_00),
            T(3, debit=100_00, bal=300_00)]
    meta = META.model_copy(update={"closing_balance": 3000_00})
    req = review(rows, meta)
    (iss,) = req.issues
    assert iss.kind == "closing_balance" and iss.severity == "fix"
    assert iss.suggestions[0].label == "Closing balance is ₹300.00"
    fixed, meta2 = apply(rows, meta, iss.suggestions[0])
    assert validate(meta2, fixed).ok
    checks = {c.key: c.status for c in req.checks}
    assert checks == {"opening": "pass", "running_balance": "pass", "totals": "fail",
                      "closing": "fail"}


def test_unclear_row_that_adds_up_is_listed_for_confirmation():
    rows = [T(1, debit=100_00, bal=900_00, ambiguous=["unknown_side"]),
            T(2, debit=500_00, bal=400_00), T(3, debit=100_00, bal=300_00)]
    req = review(rows)
    (iss,) = req.issues
    assert iss.kind == "unclear_row" and "debit or a credit" in iss.explanation


def test_request_is_plain_json_and_has_a_schema_for_the_ui():
    rows = [T(1, debit=100_00, bal=900_00, source="ocr"), T(2, debit=50_00, bal=400_00,
                                                            source="ocr"),
            T(3, debit=100_00, bal=300_00, source="ocr")]
    payload = json.loads(json.dumps(review(rows).model_dump(mode="json")))
    assert ReviewRequest.model_validate(payload).issues[0].row_idx == 1
    assert payload["issues"][0]["context"][1]["debit"] == {"paise": 5000, "text": "₹50.00"}
    assert payload["issues"][0]["context"][1]["read_by"] == "read from the scan (OCR)"
    assert "issues" in ReviewRequest.model_json_schema()["properties"]
    assert "action" in ReviewAnswer.model_json_schema()["properties"]


def test_answers_accept_rupees_as_people_type_them():
    ans = parse_answer({"action": "correct", "rows": [
        {"idx": 2, "debit": "1,234.50", "balance": "500 Dr", "date": "05/01/2025"},
        {"idx": 3, "credit": 99.5}], "meta": {"closing_balance": "₹2,00,000"}})
    edits, meta = to_internal(ans)
    assert edits[0] == {"idx": 2, "date": "2025-01-05", "debit": 123450, "balance": -50000}
    assert edits[1] == {"idx": 3, "credit": 9950}
    assert meta == {"closing_balance": 20000000}
    assert parse_answer({"accept": True}).action == "accept"  # short form
    assert parse_answer([{"idx": 0, "debit": 5}]) == [{"idx": 0, "debit": 5}]  # code form
    with pytest.raises(ValueError):
        to_internal(parse_answer({"action": "correct", "rows": [{"idx": 0, "debit": "abc"}]}))


def test_scan_confirmed_inconsistency_does_not_claim_a_text_layer():
    rows = [T(1, debit=100_00, bal=900_00, source="ocr_verified"),
            T(2, debit=50_00, bal=400_00, source="ocr_verified"),
            T(3, debit=100_00, bal=250_00, source="ocr_verified")]
    rep = validate(META, rows)
    rep.breaks[0].kind = "source_inconsistency"
    report = {"ok": False, "breaks": [b.__dict__ for b in rep.breaks], "totals_ok": None}
    req = build_review(stmt_id="s", pdf="x.pdf", meta=META, rows=rows, report=report)
    assert "scan was read again" in req.issues[0].explanation
    assert "text layer" not in req.issues[0].explanation


def test_state_with_ocr_coordinates_is_checkpoint_safe():
    """Scanned rows carry numpy floats; the Postgres checkpointer must be able to store them."""
    import numpy as np
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from app.graphs.ingestion import _dump, _plain

    t = Txn(date="2025-01-01", debit=1, balance=1, bbox=tuple(np.float64(v) for v in (1, 2, 3, 4)))
    state = {"rows": _dump([t]), "layouts": _plain([{"width": np.float64(595.0)}])}
    JsonPlusSerializer().dumps_typed(state)  # raises on numpy types


def test_closing_card_points_at_the_last_row_on_the_page():
    rows = [T(1, debit=100_00, bal=900_00), T(2, debit=500_00, bal=400_00),
            T(3, debit=100_00, bal=300_00)]
    req = review(rows, META.model_copy(update={"closing_balance": 3000_00}))
    (iss,) = req.issues
    assert iss.origin == "details" and iss.location.page == 1 and iss.location.bbox
    assert iss.context[0].row_number == 3 and "page 1" in iss.explanation


def test_many_statement_side_issues_suggest_keeping_all():
    bal, rows = 1000_00, []
    for d in range(1, 13):  # every other row prints a balance that's off by ₹7, never fixable
        bal -= 10_00
        rows.append(T(d, debit=10_00, bal=bal + (700 * d if d % 2 else 0)))
    req = review(rows, META.model_copy(update={"closing_balance": rows[-1].balance}))
    assert sum(i.origin == "statement" for i in req.issues) >= 5
    assert "keep them all as printed" in req.headline


def test_a_users_own_wrong_entry_is_called_that():
    rows = [T(1, debit=100_00, bal=900_00), T(2, debit=52_00, bal=400_00, source="user"),
            T(3, debit=100_00, bal=300_00)]
    iss = review(rows).issues[0]
    assert "values you entered" in iss.explanation and iss.severity == "fix"
    assert "statement prints" not in iss.explanation
