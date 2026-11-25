"""Rule-based anomaly detection over categorised transactions (pure code; no LLM decides).

Input rows: dicts with idx, date (ISO), merchant, category, debit, credit (paise), description.
Output: [{idx, type, score, details}]. Explanations are added separately (LLM or template).
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date

LARGE_MULTIPLE = 5.0       # debit ≥ 5× its category's median …
LARGE_MIN_PAISE = 10_000_00  # … and ≥ ₹10,000
RECURRING_CATEGORIES = {"Subscriptions", "EMI/Loans"}
MANDATE = re.compile(r"^(ACH|NACH|SI[-\s]|ECS|AUTOPAY)", re.IGNORECASE)
FEE_MIN_COUNT = 3
FEE_MULTIPLE = 3.0
FEE_MIN_PAISE = 500_00
# Fees that are routine by nature: never the "spike", even when one month can't show they recur.
ROUTINE_FEE = re.compile(r"SMS|ALERT|\bAMC\b|ANNUAL|MAINT|DEBIT CARD FEE|CARD ISSUANCE",
                         re.IGNORECASE)


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _month(s: str) -> str:
    return s[:7]


def duplicates(rows: list[dict]) -> list[dict]:
    out, seen = [], defaultdict(list)
    for r in sorted((r for r in rows if r["debit"] > 0 and r.get("date")),
                    key=lambda r: (r["date"], r["idx"])):
        key = (r.get("merchant") or r["description"], r["debit"])
        for prev in seen[key]:
            if abs((_d(r["date"]) - _d(prev["date"])).days) <= 1:
                out.append({"idx": r["idx"], "type": "duplicate_charge", "score": 0.9,
                            "details": {"duplicate_of": prev["idx"], "amount": r["debit"],
                                        "merchant": r.get("merchant")}})
                break
        seen[key].append(r)
    return out


def large_debits(rows: list[dict]) -> list[dict]:
    by_cat = defaultdict(list)
    for r in rows:
        if r["debit"] > 0 and r.get("category"):
            by_cat[r["category"]].append(r)
    out = []
    for cat, rs in by_cat.items():
        if len(rs) < 3:
            continue
        med = statistics.median(r["debit"] for r in rs)
        for r in rs:
            if med > 0 and r["debit"] >= LARGE_MULTIPLE * med and r["debit"] >= LARGE_MIN_PAISE:
                ratio = r["debit"] / med
                out.append({"idx": r["idx"], "type": "large_debit",
                            "score": round(min(1.0, ratio / 10), 2),
                            "details": {"amount": r["debit"], "category": cat,
                                        "category_median": int(med), "multiple": round(ratio, 1)}})
    return out


def new_recurring(rows: list[dict], history_merchants: set[str] | None = None) -> list[dict]:
    """A recurring-type debit in the latest month from a merchant never seen before (earlier
    months of this statement, or stored history for the account). Needs something to compare."""
    dated = [r for r in rows if r.get("date")]
    if not dated:
        return []
    months = sorted({_month(r["date"]) for r in dated})
    history = history_merchants or set()
    if len(months) < 2 and not history:
        return []
    last = months[-1]
    earlier = {r.get("merchant") for r in dated if _month(r["date"]) < last} | history
    out, flagged = [], set()
    for r in sorted(dated, key=lambda r: (r["date"], r["idx"])):
        recurring_kind = (r.get("category") in RECURRING_CATEGORIES
                          or MANDATE.match(r.get("description") or ""))
        if (_month(r["date"]) == last and r["debit"] > 0 and recurring_kind
                and r.get("merchant") not in earlier and r.get("merchant") not in flagged):
            flagged.add(r.get("merchant"))  # one flag per new merchant: its first payment
            out.append({"idx": r["idx"], "type": "new_recurring", "score": 0.6,
                        "details": {"merchant": r.get("merchant"), "amount": r["debit"],
                                    "first_seen": r["date"]}})
    return out


def fee_spikes(rows: list[dict]) -> list[dict]:
    fees = [r for r in rows if r.get("category") == "Fees/Charges" and r["debit"] > 0 and r.get("date")]
    by_month = defaultdict(list)
    for r in fees:
        by_month[_month(r["date"])].append(r)
    totals = {m: sum(r["debit"] for r in rs) for m, rs in by_month.items()}
    regular = defaultdict(set)  # amounts that recur across months (e.g. SMS charges)
    for m, rs in by_month.items():
        for r in rs:
            regular[r["debit"]].add(m)
    out = []
    for m, rs in by_month.items():
        others = [t for mm, t in totals.items() if mm != m]
        baseline = statistics.median(others) if others else 0
        if len(rs) < FEE_MIN_COUNT or totals[m] < max(FEE_MIN_PAISE, FEE_MULTIPLE * baseline):
            continue
        for r in rs:
            if len(regular[r["debit"]]) > 1 or ROUTINE_FEE.search(r.get("description") or ""):
                continue  # routine fee: recurs every month, or is routine by name
            out.append({"idx": r["idx"], "type": "fee_spike", "score": 0.7,
                        "details": {"month": m, "month_fees": totals[m], "fee_count": len(rs),
                                    "typical_month": int(baseline), "amount": r["debit"]}})
    return out


def detect(rows: list[dict], history_merchants: set[str] | None = None) -> list[dict]:
    found = (duplicates(rows) + large_debits(rows) + new_recurring(rows, history_merchants)
             + fee_spikes(rows))
    return sorted(found, key=lambda a: (a["idx"], a["type"]))


# ------------------------------------------------------------------ explanations
def _rs(paise: int) -> str:
    from app.services.review import inr

    return inr(paise)


def _month_text(m: str) -> str:
    return date.fromisoformat(m + "-01").strftime("%b %Y")


def template_explanation(a: dict, row: dict) -> str:
    d = a["details"]
    if a["type"] == "duplicate_charge":
        return (f"{_rs(d['amount'])} at {d.get('merchant') or 'the same merchant'} was charged "
                f"twice within a day.")
    if a["type"] == "large_debit":
        return (f"{_rs(d['amount'])} is {d['multiple']}x your usual {d['category']} spend "
                f"({_rs(d['category_median'])}).")
    if a["type"] == "new_recurring":
        return f"New recurring payment to {d.get('merchant')} of {_rs(d['amount'])}."
    if a["type"] == "fee_spike":
        return (f"This {_rs(d['amount'])} charge is one of {d['fee_count']} bank charges "
                f"totalling {_rs(d['month_fees'])} in {_month_text(d['month'])}, well above a "
                f"usual month.")
    return "Unusual transaction."


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def allowed_numbers(a: dict, row: dict) -> set[str]:
    vals = set()
    for v in [*a["details"].values(), row.get("date", "")]:
        if isinstance(v, int):
            vals |= {f"{v / 100:.2f}", f"{v / 100:.0f}", str(v)}
        elif isinstance(v, float):
            vals |= {f"{v}", f"{v:.1f}", f"{v:.0f}"}
        elif isinstance(v, str):
            vals |= numbers_in(v)
    return {x.replace(",", "") for x in vals}
