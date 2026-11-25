"""Parser exactness on our synthetic layouts (exact arithmetic, known ground truth)."""

from pathlib import Path

import pytest

from app.config import get_settings
from app.datasets.truth import iter_dataset
from app.parsing.digital import parse_pdf, resolve_zero_amount_rows
from app.schemas.statement import Txn
from evals.extraction import as_dicts
from evals.scoring import score_rows

SYN = get_settings().data_dir / "synthetic"
pytestmark = pytest.mark.skipif(not any(SYN.glob("*.pdf")),
                                reason="run scripts/generate_synthetic.py first")


@pytest.mark.parametrize("truth", iter_dataset("synthetic", get_settings().data_dir),
                         ids=lambda t: t.id)
def test_synthetic_exact(truth):
    res = parse_pdf(truth.pdf)
    s = score_rows(truth.rows, as_dicts(res.rows))
    assert s["exact_rows"] == s["n_truth"] == s["n_pred"]
    assert not any(t.ambiguous for t in res.rows)


def test_watermark_and_scan_have_no_rows(tmp_path: Path):
    scans = sorted(SYN.glob("*_scan.pdf"))
    if not scans:
        pytest.skip("no scans")
    res = parse_pdf(scans[0])
    assert res.rows == [] and not res.has_text_layer


def test_zero_amount_rows_resolved():
    rows = [Txn(date="2024-01-01", credit=500, balance=1500),
            Txn(date="2024-01-02", balance=1500, ambiguous=["no_amount"]),
            Txn(date="2024-01-03", balance=1400, ambiguous=["no_amount"])]
    resolve_zero_amount_rows(rows, opening=1000)
    assert rows[1].ambiguous == [] and rows[1].notes == ["zero_amount"]
    assert rows[2].ambiguous == ["no_amount"]  # balance moved: amount really is missing
