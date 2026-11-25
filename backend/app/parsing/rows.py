"""RawRow (cell text by role) → Txn (typed, paise), flagging anything ambiguous."""

from __future__ import annotations

import re

from app.parsing.layout import RawRow
from app.parsing.values import iso, parse_amount, parse_date, parse_drcr
from app.schemas.statement import Txn

IFSC_O = re.compile(r"\b([A-Z]{4})O([A-Z0-9]{6})\b")
BALANCE_MARKER = re.compile(r"(?i)^\s*(b/?f|opening\s+balance|balance\s+b/?f|brought\s+forward)\b")


def to_txn(row: RawRow) -> Txn | int:
    """Returns a Txn, or an int (paise) for an opening-balance marker row like 'B/F'."""
    c = row.cells
    amb: list[str] = []

    d = parse_date(c.get("date") or c.get("posted") or c.get("value_date") or "")
    if d is None:
        amb.append("bad_date")
    vd = parse_date(c.get("value_date", "")) if c.get("value_date") else None

    bal_txt = c.get("balance", "")
    bal = parse_amount(bal_txt)
    balance = None
    if bal is None:
        if bal_txt.strip():
            amb.append("bad_balance")
        else:
            amb.append("no_balance")
    else:
        balance = -bal.paise if bal.side == "debit" else bal.paise

    debit = credit = 0
    if "debit" in c or "credit" in c:
        dt, ct = c.get("debit", ""), c.get("credit", "")
        da, ca = parse_amount(dt), parse_amount(ct)
        for txt, val in ((dt, da), (ct, ca)):
            if txt.strip() and txt.strip() not in {"-", "–"} and val is None:
                amb.append("bad_amount")
        debit = da.paise if da else 0
        credit = ca.paise if ca else 0
        if debit and credit:
            amb.append("both_debit_credit")
    elif "amount" in c:
        at = c.get("amount", "")
        a = parse_amount(at)
        if a is None:
            amb.append("bad_amount" if at.strip() else "no_amount")
        else:
            side = parse_drcr(c.get("drcr", "")) or a.side
            if side is None:
                amb.append("unknown_side")
                side = "debit"
            debit, credit = (a.paise, 0) if side == "debit" else (0, a.paise)

    desc = re.sub(r"\s+", " ", c.get("description", "")).strip()
    desc = IFSC_O.sub(r"\g<1>0\g<2>", desc)  # IFSC 5th char is always zero; OCR reads 'O' 
    if not debit and not credit:
        if balance is not None and BALANCE_MARKER.match(desc):
            return balance
        if "no_amount" not in amb and "bad_amount" not in amb:
            amb.append("no_amount")

    return Txn(
        date=iso(d), value_date=iso(vd), description=desc, ref=(c.get("ref") or None),
        debit=debit, credit=credit, balance=balance, page=row.page, bbox=row.bbox,
        raw=dict(c), ambiguous=amb,
    )
