"""LLM extraction agents. The LLM only sees what code can't settle:
statement metadata, and rows the parser flagged (or that failed validation)."""

from __future__ import annotations

import json
import re

from app.llm.client import LLMClient
from app.parsing.values import iso, parse_amount, parse_date
from app.schemas.statement import LLMMeta, LLMRows, StatementMeta, Txn

META_SYSTEM = (
    "You extract bank statement metadata. Use only what is printed. Dates as YYYY-MM-DD "
    "(the statements use day-first dates). Amounts as plain numbers without commas or currency. "
    "A balance marked 'Dr' (or with a minus sign) is overdrawn: return it as a negative number. "
    "Use null when a field is not printed; never compute or guess a balance."
)

ROWS_SYSTEM = (
    "You fix bank statement transaction rows that a table parser was unsure about. Each input row "
    "has the cell texts exactly as printed, keyed by column. Return one output row per input row, "
    "in the same order. Dates as YYYY-MM-DD (day-first source dates). Amounts as plain numbers. "
    "A row has a debit or a credit (or neither for a zero-amount entry), never both. "
    "balance is the printed running balance (negative if overdrawn)."
)


def _paise(x: float | None) -> int | None:
    return None if x is None else round(x * 100)


def mask_account(acct: str | None) -> str | None:
    digits = "".join(ch for ch in (acct or "") if ch.isalnum())
    return digits[-4:] if digits else None


def extract_metadata(llm: LLMClient, head_text: str, tail_text: str) -> StatementMeta:
    content = (f"TEXT ABOVE THE TRANSACTION TABLE:\n{head_text[:3500]}\n\n"
               f"TEXT AFTER THE LAST TRANSACTION:\n{tail_text[:1500]}")
    m = llm.chat_json([{"role": "system", "content": META_SYSTEM},
                       {"role": "user", "content": content}], LLMMeta)
    return StatementMeta(
        bank_name=m.bank_name, account_holder=m.account_holder,
        account_masked=mask_account(m.account_number),
        period_start=iso(parse_date(m.period_start or "")),
        period_end=iso(parse_date(m.period_end or "")),
        opening_balance=_paise(m.opening_balance), closing_balance=_paise(m.closing_balance),
        currency=m.currency or "INR",
    )


def repair_rows(llm: LLMClient, rows: list[Txn], prev_balance: int | None,
                errors: list[str] | None = None) -> list[Txn]:
    """Re-extract a chunk (≤20 rows) from its printed cells. Returns rows in the same order;
    if the model returns a different number of rows, the originals are kept."""
    payload = {
        "previous_row_balance": None if prev_balance is None else prev_balance / 100,
        "rows": [t.raw for t in rows],
    }
    msg = json.dumps(payload, ensure_ascii=False)
    if errors:
        msg += "\n\nValidation errors to fix (checked in code):\n- " + "\n- ".join(errors)
    out = llm.chat_json([{"role": "system", "content": ROWS_SYSTEM},
                         {"role": "user", "content": msg}], LLMRows).rows
    if len(out) != len(rows):
        return rows
    fixed = []
    for orig, r in zip(rows, out, strict=True):
        debit, credit = abs(_paise(r.debit) or 0), abs(_paise(r.credit) or 0)
        balance = _paise(r.balance) if r.balance is not None else orig.balance
        # Guardrail: every number the LLM returns must be printed in that row's cells.
        printed = printed_amounts(orig)
        unverified = [v for v in (debit, credit, balance)
                      if v not in (0, None) and abs(v) not in printed]
        if unverified:
            fixed.append(orig.model_copy(update={
                "ambiguous": sorted({*orig.ambiguous, "llm_unverified"})}))
            continue
        fixed.append(orig.model_copy(update={
            "date": iso(parse_date(r.date)) or orig.date,
            "description": r.description or orig.description,
            "debit": debit, "credit": credit, "balance": balance,
            "ambiguous": [], "source": "llm",
        }))
    return fixed


AMOUNT_ROLES = {"debit", "credit", "amount", "balance"}


def printed_amounts(t: Txn) -> set[int]:
    """Amounts (paise, unsigned) printed in the row's amount/balance cells. Dates, times,
    references and descriptions are excluded: '01:45 PM' must not license a ₹45 debit."""
    found = set()
    for role, text in t.raw.items():
        if role not in AMOUNT_ROLES:
            continue
        for tok in re.findall(r"[\d,]+(?:\.\d{1,2})?", text):
            a = parse_amount(tok)
            if a is not None:
                found.add(a.paise)
    return found
