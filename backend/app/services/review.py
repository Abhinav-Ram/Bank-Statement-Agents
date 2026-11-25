"""Human review: what the reviewer is shown (ReviewRequest) and what they send back (ReviewAnswer).

Built entirely in code from the validation report (no LLM). It is written for a person who has
the statement PDF open next to it:

* every amount is in rupees with Indian digit grouping (₹1,23,456.50), next to its paise value;
* every problem is one Issue card: a plain-English title and explanation, the flagged row with the
  rows just before and after it (so the running balance can be followed by eye), where the row is
  on the PDF page (for a highlight box), and up to three one-click suggested fixes computed from
  the neighbouring balances, the likely one first;
* the answer is "accept" (keep as printed: the PDF really shows these numbers) or "correct" with
  edits typed in rupees as the person would read them ("1,234.50", "500 Dr").

The models are Pydantic so the Phase 4 API can expose them unchanged (OpenAPI → typed UI client).
Suggested values are arithmetic on printed numbers, shown to the person to choose; nothing is
applied without their answer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.parsing.values import iso, parse_amount, parse_date
from app.schemas.statement import StatementMeta, Txn

# ------------------------------------------------------------------ wording
READ_BY = {
    "parser": "read from the PDF's text",
    "ocr": "read from the scan (OCR)",
    "ocr_verified": "read from the scan, confirmed by a second read",
    "ocr_fixed": "read from the scan, corrected by a second read",
    "ocr_reread": "read from the scan; the re-reads disagreed",
    "llm": "repaired by the AI model",
    "user": "entered by you",
}
UNCLEAR = {
    "bad_date": "the date couldn't be read",
    "no_balance": "no balance was found",
    "bad_balance": "the balance couldn't be read as a number",
    "bad_amount": "the amount couldn't be read as a number",
    "no_amount": "no amount was found",
    "both_debit_credit": "both a debit and a credit were found",
    "unknown_side": "it isn't clear whether it is a debit or a credit",
}
WHY_BREAK = {
    "source_inconsistency": (
        "These numbers were taken exactly from the PDF's text layer, so the statement itself "
        "prints them this way. If the PDF shows the same, choose “Keep as printed”."),
    "needs_review": (
        "The scan was read more than once (and by a second OCR engine) and the readings "
        "disagree. Compare with the highlighted row on the page."),
    "retryable": "This row was hard to read and the automatic repair didn't fix it.",
}
WHY_PRINTED_TYPO = (
    "These are the numbers the statement prints, but changing just one value (the first "
    "suggestion) makes the rows around it add up, so it is most likely a typo on the statement. "
    "Correct it if you want the totals to reconcile, or keep it as printed.")
WHY_BREAK_SCAN_CONFIRMED = (
    "The scan was read again and gave the same numbers, so the statement most likely prints "
    "them this way. If the PDF shows the same, choose “Keep as printed”.")
INSTRUCTIONS = (
    "Compare each highlighted row with the statement. For each one, pick a suggested fix, type "
    "the correct values, or choose “Keep as printed” if the PDF really shows these numbers. "
    "Amounts are in rupees. Your changes are checked again before the statement is saved.")
EDITABLE_ROW = ["date", "description", "debit", "credit", "balance"]


def inr(paise: int) -> str:
    """₹ with Indian grouping: 12345678 paise → '₹1,23,456.78'."""
    rupees, p = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(([head] if head else []) + groups + [tail])
    return f"{'-' if paise < 0 else ''}₹{s}.{p:02d}"


def date_text(d: str | None) -> str | None:
    return date.fromisoformat(d).strftime("%d %b %Y") if d else None


# ------------------------------------------------------------------ what the person sees
class Money(BaseModel):
    paise: int
    text: str  # "₹1,23,456.50"


def money(paise: int | None) -> Money | None:
    return None if paise is None else Money(paise=paise, text=inr(paise))


class RowView(BaseModel):
    idx: int  # 0-based, used in answers
    row_number: int  # 1-based, for people
    role: Literal["before", "flagged", "after"]
    page: int | None
    date: str | None  # ISO
    date_text: str | None  # "05 Jan 2025"
    description: str
    debit: Money | None
    credit: Money | None
    balance: Money | None
    printed: dict[str, str]  # cell text exactly as read, by column role
    read_by: str


class Location(BaseModel):
    """Where the flagged row is on the page: bbox (x0, y0, x1, y1) in the same units as
    page_width/page_height, origin top-left. Highlight = bbox / page size × rendered size."""
    page: int
    bbox: tuple[float, float, float, float] | None
    page_width: float | None
    page_height: float | None


class RowEdit(BaseModel):
    """One row correction. Money in rupees as typed ("1,234.50", 1234.5, "500 Dr" for a
    negative balance). Leave a field out to keep it; send 0 to clear a debit/credit."""
    model_config = ConfigDict(extra="ignore")
    idx: int
    date: str | None = None
    description: str | None = None
    debit: str | float | None = None
    credit: str | float | None = None
    balance: str | float | None = None


class MetaEdit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    opening_balance: str | float | None = None
    closing_balance: str | float | None = None


class Suggestion(BaseModel):
    id: str
    label: str  # button text
    why: str
    likely: bool
    rows: list[RowEdit] = []
    meta: MetaEdit | None = None


class Issue(BaseModel):
    id: str  # stable within one request: "row-12", "closing"
    kind: Literal["balance_break", "unclear_row", "opening_balance", "closing_balance"]
    severity: Literal["fix", "check"]  # fix: probably a reading error; check: likely fine
    title: str
    explanation: str
    row_idx: int | None = None
    location: Location | None = None
    context: list[RowView] = []
    suggestions: list[Suggestion] = []
    editable: list[str] = []


class Check(BaseModel):
    key: Literal["opening", "running_balance", "totals", "closing"]
    label: str
    status: Literal["pass", "fail", "unknown"]
    detail: str


class StatementInfo(BaseModel):
    file_name: str
    bank: str | None
    account: str | None
    period_text: str | None
    opening_balance: Money | None
    closing_balance: Money | None
    rows: int
    source_type: str


class ReviewRequest(BaseModel):
    version: int = 1
    stmt_id: str | None
    headline: str
    instructions: str = INSTRUCTIONS
    statement: StatementInfo
    checks: list[Check]
    issues: list[Issue]
    # also legacy-friendly: the raw validator messages
    errors: list[str] = []


# ------------------------------------------------------------------ what the person sends back
class ReviewAnswer(BaseModel):
    """action="accept": keep everything as printed and save. action="correct": apply the edits,
    re-check, and ask again only if something still doesn't add up — unless keep_remaining, in
    which case whatever still doesn't add up is accepted as printed."""
    model_config = ConfigDict(extra="ignore")
    action: Literal["accept", "correct"]
    rows: list[RowEdit] = []
    meta: MetaEdit | None = None
    keep_remaining: bool = False
    note: str | None = None


def _paise(v: str | float | None) -> int | None:
    if v is None:
        return None
    if isinstance(v, int | float):
        return round(v * 100)
    a = parse_amount(str(v))
    if a is None:
        raise ValueError(f"not an amount: {v!r}")
    return -a.paise if a.side == "debit" else a.paise


def parse_answer(answer: object) -> ReviewAnswer | list[dict]:
    """ReviewAnswer from the UI, or the internal form (a list of paise edits) used by code."""
    if isinstance(answer, list):
        return answer
    if isinstance(answer, dict) and answer.get("accept") is True and "action" not in answer:
        return ReviewAnswer(action="accept", note=answer.get("note"))
    return ReviewAnswer.model_validate(answer)


def to_internal(ans: ReviewAnswer) -> tuple[list[dict], dict]:
    """(row corrections in paise for apply_corrections, metadata updates in paise)."""
    rows = []
    for e in ans.rows:
        c: dict = {"idx": e.idx}
        if e.date is not None:
            d = parse_date(e.date)
            if d is None:
                raise ValueError(f"row {e.idx + 1}: not a date: {e.date!r}")
            c["date"] = iso(d)
        if e.description is not None:
            c["description"] = e.description
        for f in ("debit", "credit"):
            v = _paise(getattr(e, f))
            if v is not None:
                c[f] = abs(v)
        if e.balance is not None:
            c["balance"] = _paise(e.balance)
        rows.append(c)
    meta = {}
    if ans.meta:
        for f in ("opening_balance", "closing_balance"):
            v = _paise(getattr(ans.meta, f))
            if v is not None:
                meta[f] = v
    return rows, meta


# ------------------------------------------------------------------ building the request
def _net(t: Txn) -> int:
    return t.credit - t.debit


def _view(rows: list[Txn], i: int, role: str) -> RowView:
    t = rows[i]
    return RowView(idx=i, row_number=i + 1, role=role, page=t.page, date=t.date,
                   date_text=date_text(t.date), description=t.description,
                   debit=money(t.debit) if t.debit else None,
                   credit=money(t.credit) if t.credit else None, balance=money(t.balance),
                   printed=dict(t.raw), read_by=READ_BY.get(t.source, t.source))


def _context(rows: list[Txn], i: int) -> list[RowView]:
    out = [_view(rows, i - 1, "before")] if i > 0 else []
    out.append(_view(rows, i, "flagged"))
    if i + 1 < len(rows):
        out.append(_view(rows, i + 1, "after"))
    return out


def _location(t: Txn, sizes: dict[int, tuple[float, float]]) -> Location | None:
    if t.page is None:
        return None
    w, h = sizes.get(t.page, (None, None))
    return Location(page=t.page, bbox=t.bbox, page_width=w, page_height=h)


def _rupees(paise: int) -> str:
    return f"{paise / 100:.2f}"


def _movement(t: Txn) -> str:
    if t.debit:
        return f"− {inr(t.debit)} debit"
    if t.credit:
        return f"+ {inr(t.credit)} credit"
    return "with no amount"


def _why(kind: str, t: Txn) -> str:
    if kind == "source_inconsistency" and t.source.startswith("ocr"):
        return WHY_BREAK_SCAN_CONFIRMED
    return WHY_BREAK.get(kind, "")


def _break_issue(rows: list[Txn], b: dict, breaks: set[int], meta: StatementMeta,
                 sizes: dict, prev_break: dict | None = None) -> Issue:
    i, t = b["idx"], rows[b["idx"]]
    expected, printed = b["expected"], b["printed"]
    prev = expected - _net(t)
    prev_what = "the opening balance" if i == 0 else f"row {i}'s balance"
    nxt = rows[i + 1] if i + 1 < len(rows) else None
    # Which value is wrong? The next row (or, on the last row, the closing balance) decides.
    if nxt is not None:
        next_agrees = i + 1 not in breaks  # next row follows from the printed balance
        fixes_next = (i + 1 in breaks and nxt.balance is not None
                      and expected + _net(nxt) == nxt.balance)
    else:
        next_agrees = meta.closing_balance is not None and meta.closing_balance == printed
        fixes_next = meta.closing_balance is not None and meta.closing_balance == expected
    sugg = []
    amt = printed - prev
    if amt != 0:
        side = "credit" if amt > 0 else "debit"
        other = "debit" if side == "credit" else "credit"
        sugg.append(Suggestion(
            id=f"row-{i}-amount", likely=next_agrees,
            label=f"Amount is {inr(abs(amt))} {side} (balance is right)",
            why=("Keeps the printed balance" + (", which the next row agrees with."
                                                if nxt is not None and next_agrees else ".")),
            rows=[RowEdit(idx=i, **{side: _rupees(abs(amt)), other: "0"})]))
    sugg.append(Suggestion(
        id=f"row-{i}-balance", likely=fixes_next,
        label=f"Balance is {inr(expected)} (amount is right)",
        why=f"The previous balance {_movement(t)}" + (
            (f"; this also makes row {i + 2} add up." if nxt is not None
             else "; this matches the closing balance.") if fixes_next else "."),
        rows=[RowEdit(idx=i, balance=_rupees(expected))]))
    if i == 0 and meta.opening_balance is not None:
        sugg.append(Suggestion(
            id="opening-from-row-0", likely=False,
            label=f"Opening balance is {inr(printed - _net(t))}",
            why="The first row is right and the opening balance was misread.",
            meta=MetaEdit(opening_balance=_rupees(printed - _net(t)))))
    # Knock-on: the previous row's balance is the wrong one, and correcting it makes this row
    # add up. Then this card only confirms; the decision belongs to the previous row.
    knock_on = (prev_break is not None and t.balance is not None
                and prev_break["expected"] + _net(t) == t.balance)
    if knock_on:
        for sg in sugg:
            sg.likely = False
        return Issue(
            id=f"row-{i}", kind="balance_break", severity="check",
            title=f"Row {i + 1}: follows from row {i}",
            explanation=(f"This row doesn't add up only because row {i}'s balance looks wrong: "
                         f"if row {i}'s balance is {inr(prev_break['expected'])}, this row's "
                         f"numbers are right. Decide row {i} first; this row usually needs no "
                         "change."),
            row_idx=i, location=_location(t, sizes), context=_context(rows, i),
            suggestions=sugg, editable=EDITABLE_ROW)
    sugg.sort(key=lambda s: not s.likely)
    printed_typo = b["kind"] == "source_inconsistency" and sugg[0].likely
    why = WHY_PRINTED_TYPO if printed_typo else _why(b["kind"], t)
    expl = (f"Starting from {prev_what} {inr(prev)} {_movement(t)} gives {inr(expected)}, but "
            f"this row's balance reads {inr(printed)} (a difference of "
            f"{inr(abs(printed - expected))}). " + why)
    return Issue(id=f"row-{i}", kind="balance_break",
                 severity="check" if b["kind"] == "source_inconsistency" and not printed_typo
                 else "fix",
                 title=f"Row {i + 1}: the balance doesn't add up", explanation=expl, row_idx=i,
                 location=_location(t, sizes), context=_context(rows, i), suggestions=sugg,
                 editable=EDITABLE_ROW)


def _unclear_issue(rows: list[Txn], i: int, sizes: dict) -> Issue:
    t = rows[i]
    reasons = [UNCLEAR.get(r, r.replace("_", " ")) for r in t.ambiguous]
    return Issue(id=f"row-{i}", kind="unclear_row", severity="check",
                 title=f"Row {i + 1}: part of this row couldn't be read",
                 explanation=("On this row " + "; ".join(reasons) + ". The running balance "
                              "still adds up, so this is probably fine — please confirm."),
                 row_idx=i, location=_location(t, sizes), context=_context(rows, i),
                 editable=EDITABLE_ROW)


def build_review(*, stmt_id: str | None, pdf: str, meta: StatementMeta, rows: list[Txn],
                 report: dict, layouts: list[dict] | None = None,
                 source_type: str = "digital") -> ReviewRequest:
    sizes = {lay["page"]: (lay["width"], lay["height"]) for lay in layouts or []
             if lay.get("width")}
    breaks = report.get("breaks", [])
    break_idx = {b["idx"] for b in breaks}
    by_idx = {b["idx"]: b for b in breaks}
    issues = [_break_issue(rows, b, break_idx, meta, sizes, by_idx.get(b["idx"] - 1))
              for b in breaks]
    issues += [_unclear_issue(rows, i, sizes) for i, t in enumerate(rows)
               if t.ambiguous and i not in break_idx]

    credits = sum(t.credit for t in rows)
    debits = sum(t.debit for t in rows)
    last = rows[-1].balance if rows else None
    implied = (meta.opening_balance + credits - debits
               if meta.opening_balance is not None else None)
    later = " This may sort itself out once the rows above are fixed." if breaks else ""
    if report.get("totals_ok") is False:
        sugg = []
        if last is not None and last == implied:
            sugg.append(Suggestion(
                id="closing-from-rows", likely=True,
                label=f"Closing balance is {inr(last)}",
                why="Matches the last row's balance and the totals.",
                meta=MetaEdit(closing_balance=_rupees(last))))
        issues.append(Issue(
            id="closing", kind="closing_balance", severity="check" if breaks else "fix",
            title="The totals don't match the closing balance",
            explanation=(f"Opening {inr(meta.opening_balance)} + credits {inr(credits)} − "
                         f"debits {inr(debits)} = {inr(implied)}, but the statement's closing "
                         f"balance is {inr(meta.closing_balance)}." + later),
            suggestions=sugg, editable=["opening_balance", "closing_balance"]))
    elif report.get("closing_matches_last") is False:
        issues.append(Issue(
            id="closing", kind="closing_balance", severity="fix",
            title="The closing balance differs from the last row",
            explanation=(f"The last row's balance is {inr(last)} but the closing balance reads "
                         f"{inr(meta.closing_balance)}."),
            suggestions=[Suggestion(id="closing-from-rows", likely=True,
                                    label=f"Closing balance is {inr(last)}",
                                    why="Matches the last row's balance.",
                                    meta=MetaEdit(closing_balance=_rupees(last)))],
            editable=["closing_balance"]))

    def status(v: bool | None) -> str:
        return "unknown" if v is None else ("pass" if v else "fail")

    n_rows_bad = len(break_idx)
    checks = [
        Check(key="opening", label="First row follows from the opening balance",
              status=status(report.get("opening_ok")),
              detail=("No opening balance found." if meta.opening_balance is None
                      else f"Opening balance {inr(meta.opening_balance)}.")),
        Check(key="running_balance", label="Running balance adds up on every row",
              status="pass" if not breaks else "fail",
              detail=(f"All {len(rows)} rows add up." if not breaks else
                      f"{n_rows_bad} of {len(rows)} rows don't add up.")),
        Check(key="totals", label="Opening + credits − debits = closing balance",
              status=status(report.get("totals_ok")),
              detail=f"Credits {inr(credits)}, debits {inr(debits)}."),
        Check(key="closing", label="Closing balance equals the last row's balance",
              status=status(report.get("closing_matches_last")),
              detail=("No closing balance found." if meta.closing_balance is None
                      else f"Closing balance {inr(meta.closing_balance)}.")),
    ]
    n_fix = sum(i.severity == "fix" for i in issues)
    if not issues:
        headline = "Nothing to fix — this statement adds up."
    elif n_fix == 0:
        headline = (f"{len(issues)} item{'s' if len(issues) > 1 else ''} to confirm. The PDF "
                    "itself seems to print these numbers; “Keep as printed” is likely right.")
    else:
        headline = (f"{n_fix} item{'s' if n_fix > 1 else ''} likely wrong, "
                    f"{len(issues) - n_fix} to confirm. Please check them against the PDF.")
    period = (f"{date_text(meta.period_start)} – {date_text(meta.period_end)}"
              if meta.period_start and meta.period_end else None)
    info = StatementInfo(file_name=Path(pdf).name, bank=meta.bank_name,
                         account=meta.account_masked, period_text=period,
                         opening_balance=money(meta.opening_balance),
                         closing_balance=money(meta.closing_balance), rows=len(rows),
                         source_type=source_type)
    return ReviewRequest(stmt_id=stmt_id, headline=headline, statement=info, checks=checks,
                         issues=issues, errors=report.get("errors", []))
