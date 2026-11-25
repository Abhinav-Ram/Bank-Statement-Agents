"""Row-level scoring of extracted transactions against ground truth.

Rows are dicts with date (ISO), description, debit, credit, balance (paise ints, 0/None = empty).
Alignment is order-preserving (LCS) on (date, amount) so a wrong debit/credit side or balance
shows up as a field error, while a wrong date/amount or a split/merged row shows up as a
missing + extra row.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

FIELDS = ("date", "description", "debit", "credit", "balance")


def _key(r: dict) -> tuple:
    return (r.get("date"), (r.get("debit") or 0) + (r.get("credit") or 0))


def norm_desc(s: str | None) -> str:
    return re.sub(r"\s+", "", (s or "")).upper()


def _eq(field: str, a: dict, b: dict) -> bool:
    if field == "description":
        return norm_desc(a.get(field)) == norm_desc(b.get(field))
    if field in ("debit", "credit"):
        return (a.get(field) or 0) == (b.get(field) or 0)
    return a.get(field) == b.get(field)


def align_rows(truth: list[dict], pred: list[dict]) -> list[tuple[int, int]]:
    sm = SequenceMatcher(a=[_key(r) for r in truth], b=[_key(r) for r in pred], autojunk=False)
    return [(blk.a + k, blk.b + k) for blk in sm.get_matching_blocks() for k in range(blk.size)]


def score_rows(truth: list[dict], pred: list[dict]) -> dict:
    pairs = align_rows(truth, pred)
    m = len(pairs)
    correct = {f: sum(_eq(f, truth[i], pred[j]) for i, j in pairs) for f in FIELDS}
    exact = sum(all(_eq(f, truth[i], pred[j]) for f in FIELDS) for i, j in pairs)
    return {
        "n_truth": len(truth),
        "n_pred": len(pred),
        "matched": m,
        "row_recall": m / len(truth) if truth else 1.0,
        "row_precision": m / len(pred) if pred else (1.0 if not truth else 0.0),
        "exact_rows": exact,
        "field_acc": {f: (correct[f] / m if m else 0.0) for f in FIELDS},
    }
