"""Reconciliation checks (pure code; the LLM never decides whether numbers add up).

Checks:
  opening_ok : first row's balance - its net amount == stated opening balance
  breaks     : rows where prev printed balance + credit - debit != printed balance
  totals_ok  : opening + credits - debits == stated closing balance
A debit/credit swap that closes a break is fixed in place. A break on a cleanly parsed
row is a `source_inconsistency` (what is printed doesn't add up); a break on an ambiguous or
LLM-produced row is `retryable`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.statement import StatementMeta, Txn


@dataclass
class Break:
    idx: int
    expected: int
    printed: int
    kind: str  # "retryable" | "source_inconsistency"


@dataclass
class Report:
    ok: bool
    opening_ok: bool | None
    totals_ok: bool | None
    closing_matches_last: bool | None
    breaks: list[Break] = field(default_factory=list)
    fixes: list[tuple[int, str]] = field(default_factory=list)
    n_rows: int = 0

    @property
    def retry_idx(self) -> list[int]:
        return [b.idx for b in self.breaks if b.kind == "retryable"]

    def errors(self) -> list[str]:
        out = []
        if self.opening_ok is False:
            out.append("first row does not follow from the stated opening balance")
        if self.totals_ok is False:
            out.append("opening + credits - debits != closing balance")
        out += [f"row {b.idx}: expected balance {b.expected / 100:.2f}, got {b.printed / 100:.2f}"
                for b in self.breaks]
        return out


def _break_kind(t: Txn) -> str:
    if t.ambiguous or t.source in ("llm", "ocr"):
        return "retryable"
    if t.source == "ocr_reread":  # engines disagree and no reading closes the chain
        return "needs_review"
    return "source_inconsistency"  # clean text layer, or every OCR engine read the same values


def _net(t: Txn) -> int:
    return t.credit - t.debit


def validate(meta: StatementMeta, rows: list[Txn]) -> Report:
    if not rows:
        return Report(ok=False, opening_ok=None, totals_ok=None, closing_matches_last=None)

    fixes: list[tuple[int, str]] = []
    breaks: list[Break] = []
    opening = meta.opening_balance
    first = rows[0]
    # Without a stated opening balance, derive it from the first row (row 0 then can't break).
    prev = opening if opening is not None else (
        first.balance - _net(first) if first.balance is not None else None)

    for i, t in enumerate(rows):
        if t.balance is None or prev is None:
            prev = t.balance
            continue
        if prev + _net(t) != t.balance:
            if (t.debit or t.credit) and prev - _net(t) == t.balance:
                t.debit, t.credit = t.credit, t.debit
                fixes.append((i, "swapped_debit_credit"))
            else:
                kind = _break_kind(t)
                breaks.append(Break(i, prev + _net(t), t.balance, kind))
        prev = t.balance

    opening_ok = None
    if opening is not None and first.balance is not None:
        opening_ok = not any(b.idx == 0 for b in breaks)

    total = sum(_net(t) for t in rows)
    totals_ok = None
    if meta.opening_balance is not None and meta.closing_balance is not None:
        totals_ok = meta.opening_balance + total == meta.closing_balance
    last_bal = rows[-1].balance
    closing_matches_last = (None if meta.closing_balance is None or last_bal is None
                            else meta.closing_balance == last_bal)
    ok = not breaks and totals_ok is not False and closing_matches_last is not False
    return Report(ok, opening_ok, totals_ok, closing_matches_last, breaks, fixes, len(rows))
