from evals.scoring import align_rows, score_rows


def r(d, debit=0, credit=0, bal=0, desc="X"):
    return {"date": d, "description": desc, "debit": debit, "credit": credit, "balance": bal}


TRUTH = [r("2024-01-01", credit=100, bal=100, desc="SALARY JAN"),
         r("2024-01-02", debit=40, bal=60, desc="UPI/123/ZOMATO"),
         r("2024-01-02", debit=40, bal=20, desc="UPI/123/ZOMATO")]


def test_perfect():
    s = score_rows(TRUTH, [dict(x) for x in TRUTH])
    assert s["row_recall"] == 1 and s["row_precision"] == 1 and s["exact_rows"] == 3
    assert s["field_acc"]["balance"] == 1


def test_description_whitespace_insensitive():
    pred = [dict(x) for x in TRUTH]
    pred[1]["description"] = "UPI/123/ ZOMATO"
    assert score_rows(TRUTH, pred)["field_acc"]["description"] == 1


def test_missing_and_extra_rows():
    pred = [dict(TRUTH[0]), dict(TRUTH[2]), r("2024-01-03", debit=5, bal=15)]
    s = score_rows(TRUTH, pred)
    assert s["matched"] == 2 and s["row_recall"] == 2 / 3 and s["row_precision"] == 2 / 3


def test_wrong_balance_counts_as_field_error_not_missing_row():
    pred = [dict(x) for x in TRUTH]
    pred[2]["balance"] = 21
    s = score_rows(TRUTH, pred)
    assert s["matched"] == 3 and s["exact_rows"] == 2 and s["field_acc"]["balance"] == 2 / 3


def test_alignment_is_order_preserving():
    pairs = align_rows(TRUTH, [dict(TRUTH[1]), dict(TRUTH[0])])
    assert len(pairs) == 1
