"""Ground-truth loaders → one normalized shape: (StatementMeta, rows as dicts in paise).

AgamiAI Type1: debit/credit/balance, date 'YYYY-MM-DD hh:mm:ss'.
AgamiAI Type2: cr_dr + transaction_amount + available_balance, dates 'dd/mm/yyyy'.
Synthetic (ours): debit/credit/balance in rupees, ISO dates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.parsing.values import iso, parse_date
from app.schemas.statement import StatementMeta


def _p(x) -> int:
    return 0 if x in (None, "", "-") else round(float(x) * 100)


@dataclass
class Truth:
    id: str
    dataset: str  # agami_digital_t1 | agami_scanned_t2 | synthetic | ...
    pdf: Path
    meta: StatementMeta
    rows: list[dict]


def load_json_truth(json_path: Path, pdf: Path, dataset: str) -> Truth:
    j = json.loads(json_path.read_text())
    rows = []
    for t in j["transactions"]:
        if "transaction_amount" in t:
            amt = _p(t["transaction_amount"])
            dr = str(t.get("cr_dr", "")).upper().startswith("D")
            debit, credit = (amt, 0) if dr else (0, amt)
            bal = _p(t["available_balance"])
        else:
            debit, credit, bal = _p(t.get("debit")), _p(t.get("credit")), _p(t.get("balance"))
        rows.append({"date": iso(parse_date(str(t["date"]))), "description": t["description"],
                     "debit": debit, "credit": credit, "balance": bal,
                     "failed": bool(t.get("failed", False))})
    acct = str(j.get("account_number") or j.get("account_masked") or "")
    meta = StatementMeta(
        bank_name=j.get("bank_name") or j.get("bank"),
        account_holder=j.get("account_holder") or j.get("holder"),
        account_masked=acct[-4:] if acct else None,
        period_start=iso(parse_date(str(j.get("start_date") or j.get("period_start") or ""))),
        period_end=iso(parse_date(str(j.get("end_date") or j.get("period_end") or ""))),
        opening_balance=_p(j.get("opening_balance")),
        closing_balance=_p(j.get("closing_balance")),
        currency=j.get("currency", "INR"),
    )
    return Truth(json_path.stem, dataset, pdf, meta, rows)


def iter_dataset(name: str, data_dir: Path, limit: int | None = None) -> list[Truth]:
    """name: synthetic | synthetic_scan | agami_digital_t1 | agami_digital_t2 |
    agami_scanned_t1 | agami_scanned_t2 | agami_digital (both types) | agami_scanned"""
    out: list[Truth] = []
    if name.startswith("synthetic"):
        d = data_dir / "synthetic"
        for j in sorted(d.glob("*.json")):
            if j.name == "manifest.json":
                continue
            pdf = d / (f"{j.stem}_scan.pdf" if name == "synthetic_scan" else f"{j.stem}.pdf")
            if pdf.exists():
                out.append(load_json_truth(j, pdf, name))
    else:
        kind = "Scanned" if "scanned" in name else "Digital"
        types = [name[-1]] if name[-2:] in ("t1", "t2") else ["1", "2"]
        for ty in types:
            d = data_dir / "agami" / "train" / f"India_Bank_Statement_{kind}_Type{ty}"
            files = sorted(d.glob("*.json"))
            if limit and len(types) > 1:
                files = files[: (limit + 1) // 2]
            for j in files:
                t = load_json_truth(j, j.with_suffix(".pdf"), f"agami_{kind.lower()}_t{ty}")
                t.id = f"t{ty}_{j.stem}"
                out.append(t)
    return out[:limit] if limit else out
