"""Summaries: every number is computed in code (fact sheet); the LLM only words it, and a checker
rejects any narrative containing a number that isn't in the fact sheet."""

from __future__ import annotations

import json
import re
from collections import defaultdict

from app.llm.client import LLMClient
from app.schemas.taxonomy import HEAD_OF

MAX_WORDS = 120


def _r(paise: float) -> float:
    return round(paise / 100, 2)


def fact_sheet(rows: list[dict], anomalies: list[dict], meta: dict) -> dict:
    """rows: {date, merchant, category, debit, credit} (paise). Output amounts in rupees."""
    income = sum(r["credit"] for r in rows)
    expense = sum(r["debit"] for r in rows)
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    months: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    merchants: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        amt = r["credit"] or r["debit"]
        if not amt:
            continue
        cat = r.get("category") or ("Other Income" if r["credit"] else "Other Expense")
        by_cat[cat][0] += amt
        by_cat[cat][1] += 1
        if r.get("date"):
            months[r["date"][:7]][0 if r["credit"] else 1] += amt
        if r["debit"] and r.get("merchant"):
            merchants[r["merchant"]][0] += r["debit"]
            merchants[r["merchant"]][1] += 1
    cats = sorted(by_cat.items(), key=lambda kv: -kv[1][0])
    monthly = []
    prev_exp = None
    for m in sorted(months):
        inc, exp = months[m]
        row = {"month": m, "income": _r(inc), "expense": _r(exp), "net": _r(inc - exp)}
        if prev_exp:
            row["expense_change_pct"] = round((exp - prev_exp) / prev_exp * 100, 1)
        prev_exp = exp
        monthly.append(row)
    return {
        "period": {"start": meta.get("period_start"), "end": meta.get("period_end")},
        "opening_balance": _r(meta["opening_balance"]) if meta.get("opening_balance") is not None
        else None,
        "closing_balance": _r(meta["closing_balance"]) if meta.get("closing_balance") is not None
        else None,
        "totals": {"income": _r(income), "expense": _r(expense), "net": _r(income - expense),
                   "transactions": sum(1 for r in rows if r["credit"] or r["debit"])},
        "by_category": [{"category": c, "head": HEAD_OF.get(c, "Expense"), "total": _r(v[0]),
                         "count": v[1],
                         "share_pct": round(v[0] / (income if HEAD_OF.get(c) == "Income"
                                                    else expense) * 100, 1)
                         if (income if HEAD_OF.get(c) == "Income" else expense) else 0.0}
                        for c, v in cats],
        "monthly": monthly,
        "top_merchants": [{"merchant": m, "total": _r(v[0]), "count": v[1]}
                          for m, v in sorted(merchants.items(), key=lambda kv: -kv[1][0])[:5]],
        "anomalies": [{"type": a["type"], "date": a.get("date"), "merchant": a.get("merchant"),
                       "amount": _r(a.get("amount", 0))} for a in anomalies],
    }


# ------------------------------------------------------------------ number check
_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def narrative_numbers(text: str) -> list[str]:
    return [n.replace(",", "").lstrip("-") for n in _NUM.findall(text)]


def allowed(fs: dict) -> set[str]:
    out: set[str] = set()

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int | float):
            x = abs(float(v))
            out.update({f"{x:.2f}", f"{x:.1f}", str(int(x)) if x == int(x) else ""})
        elif isinstance(v, str):
            out.update(n.lstrip("0") or "0" for n in re.findall(r"\d+", v))
            out.update(re.findall(r"\d+", v))
        elif isinstance(v, dict):
            for x in v.values():
                add(x)
        elif isinstance(v, list):
            for x in v:
                add(x)

    add(fs)
    out.discard("")
    return out


def check(text: str, fs: dict) -> list[str]:
    """Numbers in the narrative that aren't in the fact sheet."""
    ok = allowed(fs)
    def variants(n: str) -> set[str]:
        return {n, n.rstrip("0").rstrip(".")} if "." in n else {n}

    return [n for n in narrative_numbers(text) if not variants(n) & ok]


# ------------------------------------------------------------------ narrative
SYSTEM = (f"Write a short summary (max {MAX_WORDS} words) of this bank statement for the account "
          "holder, from the fact sheet only: income vs spending, the biggest categories, how "
          "spending changed month to month, and any flagged items. Use rupee amounts exactly as "
          "they appear in the fact sheet (you may add thousands separators). Do not compute, round "
          "or invent any number. Plain text, no headings.")


def _money(rupees: float | str) -> str:
    from app.services.review import inr

    return inr(round(float(rupees) * 100))  # fact-sheet values may be decimal strings


def _month_text(m: str) -> str:
    from datetime import date

    return date.fromisoformat(m + "-01").strftime("%b %Y")


def template_narrative(fs: dict) -> str:
    t = fs["totals"]
    parts = [(f"Income was {_money(t['income'])} and spending {_money(t['expense'])} "
             f"(net {_money(t['net'])}) across {t['transactions']} transactions.")]
    exp = [c for c in fs["by_category"] if c["head"] == "Expense"][:3]
    if exp:
        parts.append("Biggest spending: " + ", ".join(
            f"{c['category']} {_money(c['total'])}" for c in exp) + ".")
    changes = [m for m in fs["monthly"] if "expense_change_pct" in m]
    if changes:
        m = changes[-1]
        pct = m["expense_change_pct"]
        parts.append(f"Spending in {_month_text(m['month'])} {'rose' if pct >= 0 else 'fell'} "
                     f"by {abs(pct)}% from the previous month.")
    if fs["anomalies"]:
        parts.append(f"{len(fs['anomalies'])} item(s) were flagged for a closer look.")
    return " ".join(parts)


def narrate(fs: dict, llm: LLMClient | None) -> tuple[str, str]:
    """Returns (narrative, source): llm | llm_regenerated | template."""
    if llm is None:
        return template_narrative(fs), "template"
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(fs, ensure_ascii=False)}]
    for attempt in range(2):
        try:
            text = llm.chat(msgs, max_tokens=300).strip()
        except Exception:  # noqa: BLE001
            break
        bad = check(text, fs)
        if text and not bad and len(text.split()) <= MAX_WORDS * 1.3:
            return text, "llm" if attempt == 0 else "llm_regenerated"
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": "These numbers are not in the fact sheet: "
                  f"{', '.join(bad[:10]) or '(too long)'}. Rewrite using only fact-sheet numbers."}]
    return template_narrative(fs), "template"
