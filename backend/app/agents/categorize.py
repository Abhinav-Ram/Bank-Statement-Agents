"""Categorisation: merchant normalisation → rules → merchant cache → LLM for leftovers only.

The LLM sees each unknown merchant once (deduplicated, batches of ≤25), picks from the fixed
taxonomy enum with a confidence, and its answer is cached so it's never asked again.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field, create_model

from app.llm.client import LLMClient
from app.schemas.statement import Txn
from app.schemas.taxonomy import ALL_CATEGORIES, EXPENSE, HEAD_OF, INCOME

BATCH = 25

# ------------------------------------------------------------------ merchant normalisation
_DROP = re.compile(r"^(?:\d{5,}|[A-Z]{4}0[A-Z0-9]{6}|[A-Z0-9]{3,5}|NONE|UPI-?TRANSFER.*|"
                   r"(?:CR|DR|P2A|P2M|BRN))$")
_PATTERNS: list[tuple[re.Pattern, int | None]] = [
    (re.compile(r"^UPI/(?:CR|DR)/\d+/([^/]+)"), 1),               # UPI/DR/7283.../PAYEL B/...
    (re.compile(r"^UPI/\d+/([^/]+)"), 1),                          # UPI/1234.../ZEPTO MKTPL/x@y
    (re.compile(r"^IMPS/P2[AM]/\d+/([^/]+)"), 1),                  # IMPS/P2A/1234/PRESTIGE/RENT
    (re.compile(r"^(?:BILLPAY|REFUND)/([^/]+)"), 1),               # BILLPAY/AIRTEL POSTPAID/..
    (re.compile(r"^ACH D-\s*([A-Z0-9 &.]+?)-\d+"), 1),             # ACH D- HDB FINANCIAL SERV-123
    (re.compile(r"^POS\s+\S+\s+(.+?)(?:\s+0?(?=[A-Z]*\d)[A-Z0-9]{4})?$"), 1),  # POS AB7M APOLLO PHARMACY 0AB7M
    (re.compile(r"TRF BY-(.+)$"), 1),                              # IMPS BRN SALARY TRF BY-X LTD
    (re.compile(r"^(?:NEFT|IMPS|RTGS)\s*(?:CR|DR|Cr|Dr)?-[^-]*-(?:[A-Z]{4}0[A-Z0-9]{6}-)?(.+?)"
                r"(?:-SALARY.*)?-*$"), 1),                         # NEFT Cr-123-ICIC0SF0002-X LTD--
    (re.compile(r"^(?:NEFT|RTGS|IMPS)-[A-Z0-9]+-(.+)$"), 1),       # NEFT-4036...-Ganesh Pillai
    (re.compile(r"(?i)^chq paid-(?:micr inward clearing-)?([^-]+)"), 1),
    (re.compile(r"(?i)^cash-bna-self-(.+)$"), 1),
    (re.compile(r"(?i)^by clg:[^-]*-(.+)$"), 1),
]
_FIXED = [  # narration keyword → fixed merchant key
    (re.compile(r"(?i)\b(ATM|ATW|CASH WDL|CASH WITHDRAWAL)\b"), "ATM CASH"),
    (re.compile(r"(?i)\b(CHGS|CHARGES|CHARGE)\b"), "BANK CHARGES"),
    (re.compile(r"(?i)\bINTEREST\b"), "BANK INTEREST"),
]


def normalise_merchant(description: str) -> str:
    d = re.sub(r"\s+", " ", (description or "").strip())
    d = re.sub(r"(?i)^(FAILED-[A-Z ]+-|REVERSAL-)", "", d)
    for pat, key in _FIXED:
        if pat.search(d):
            return key
    for pat, grp in _PATTERNS:
        m = pat.search(d)
        if m:
            name = m.group(grp)
            break
    else:
        name = d
    name = re.sub(r"\*.*$", "", name)          # AMZN MKTP IN*2K4 → AMZN MKTP IN
    name = re.sub(r"@\S+", "", name)            # handles
    parts = [p for p in re.split(r"[-/]", name) if p.strip()]
    parts = [p.strip() for p in parts if not _DROP.match(p.strip().upper())]
    name = (parts[0] if parts else name).strip(" .-")
    name = re.sub(r"\s+(IN|INDIA|PVT|PRIVATE|LTD|LIMITED|LLP)\.?$", "", name.upper()).strip()
    return name[:60] or "UNKNOWN"


# ------------------------------------------------------------------ rules
_RULES: list[tuple[str, str]] = [  # (regex on narration + merchant, category); first match wins
    (r"\b(CHGS|CHARGES|CHARGE|MIN BAL)\b", "Fees/Charges"),
    (r"\bINTEREST\b", "Interest"),
    (r"\bDIVIDEND\b", "Other Income"),
    (r"\b(REFUND|REVERSAL)\b", "Refunds"),
    (r"\bSALARY\b|\bPAYROLL\b", "Salary"),
    (r"\b(FREELANCE|UPWORK|TOPTAL|FIVERR)\b", "Business/Freelance"),
    (r"\b(ATM CASH|CASH WDL|CASH WITHDRAWAL|CASH-BNA-SELF)\b", "Cash Withdrawal"),
    (r"\b(CASH-BNA|CASH DEP|CASH DEPOSIT)", "Transfers In"),  # cash paid into the account
    (r"\bRENT\b|RENTALS|PROPERTIES", "Housing"),
    (r"\b(EMI|LOAN|HDB FINANCIAL|BAJAJ FIN)", "EMI/Loans"),
    (r"\b(LIC|INSURANCE|POLICY)\b", "Insurance"),
    (r"NETFLIX|SPOTIFY|YOUTUBE|HOTSTAR|AUDIBLE|CULTFIT|OPENAI|CHATGPT|PRIME VIDEO", "Subscriptions"),
    (r"BESCOM|ELECTRICITY|AIRTEL|JIO|VODAFONE|FIBERNET|BROADBAND|WATER|\bGAS\b|UTILITY",
     "Utilities"),
    (r"BIGBASKET|DMART|AVENUE SUPERMARTS|ZEPTO|BLINKIT|GROCER|SUPERMARKET", "Groceries"),
    (r"SWIGGY|ZOMATO|COFFEE|RESTAURANT|\bCAFE\b|DOMINOS|MCDONALD", "Dining"),
    (r"\bUBER\b|\bOLA\b|IOCL|INDIAN OIL|HPCL|BPCL|\bFUEL\b|BMRCL|\bMETRO\b|RAPIDO", "Transport/Fuel"),
    (r"AMAZON|AMZN|FLIPKART|MYNTRA|CROMA|AJIO|NYKAA", "Shopping"),
    (r"PHARMACY|APOLLO|HOSPITAL|MEDICAL|CLINIC|HEALTHCARE", "Health"),
    (r"SCHOOL|COLLEGE|UNIVERSITY|TUITION|COURSE", "Education"),
]
_RULES_RE = [(re.compile(p, re.IGNORECASE), c) for p, c in _RULES]
_COMPANY = re.compile(r"\b(LTD|LIMITED|PVT|LLP|INDUSTRIES|TECHNOLOGIES|SOLUTIONS|SERVICES|"
                      r"CORP|ENTERPRISES|TRADERS)\b", re.IGNORECASE)
_TRANSFER = re.compile(r"^(UPI|IMPS|NEFT|RTGS|CHQ|BY CLG)", re.IGNORECASE)


def direction(t: Txn) -> str:
    return "credit" if t.credit > t.debit else "debit"


def fits(category: str, t: Txn) -> bool:
    return HEAD_OF[category] == ("Income" if direction(t) == "credit" else "Expense")


_BANK_NAME = re.compile(r"\b[A-Z]+ BANK (LTD|LIMITED)\.?", re.IGNORECASE)  # the clearing bank, not a party


def rule_category(t: Txn, merchant: str) -> tuple[str, float] | None:
    text = _BANK_NAME.sub("", f"{t.description} {merchant}")
    for pat, cat in _RULES_RE:
        if pat.search(text) and fits(cat, t):
            return cat, 0.9
    if direction(t) == "credit" and _COMPANY.search(text):
        return "Business/Freelance", 0.6  # payment from a company into the account
    if _TRANSFER.match(t.description or "") and not _COMPANY.search(text):
        return ("Transfers In" if direction(t) == "credit" else "Transfers Out"), 0.6
    return None


# ------------------------------------------------------------------ LLM for leftovers
CategoryName = Literal[tuple(ALL_CATEGORIES)]  # type: ignore[valid-type]


class _Pick(BaseModel):
    merchant: str
    category: CategoryName  # type: ignore[valid-type]
    confidence: float = Field(ge=0, le=1)


LLMCategories = create_model("LLMCategories", items=(list[_Pick], ...))

SYSTEM = ("You categorise Indian bank transactions. For each merchant pick exactly one category "
          "from the allowed list and give a confidence 0-1. Credits must use an Income category "
          "and debits an Expense category. Allowed Income: " + ", ".join(INCOME) +
          ". Allowed Expense: " + ", ".join(EXPENSE) + ".")


def llm_categories(llm: LLMClient, items: list[dict]) -> dict[str, tuple[str, float]]:
    out: dict[str, tuple[str, float]] = {}
    for k in range(0, len(items), BATCH):
        batch = items[k:k + BATCH]
        res = llm.chat_json([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
                            LLMCategories)
        allowed = {b["merchant"]: b["direction"] for b in batch}
        for p in res.items:
            d = allowed.get(p.merchant)
            if d and HEAD_OF[p.category] == ("Income" if d == "credit" else "Expense"):
                out[p.merchant] = (p.category, float(p.confidence))
    return out


# ------------------------------------------------------------------ orchestration
def categorize(rows: list[Txn], cache_get: Callable[[list[str]], dict[str, tuple[str, float]]],
               cache_put: Callable[[list[tuple[str, str, float, str]]], None],
               llm: LLMClient | None) -> list[dict]:
    """Returns one assignment per row: {idx, merchant, category, confidence, source}."""
    merchants = [normalise_merchant(t.description) for t in rows]
    out: list[dict] = []
    leftovers: dict[tuple[str, str], list[int]] = {}
    for i, (t, m) in enumerate(zip(rows, merchants, strict=True)):
        if not t.debit and not t.credit:  # zero-amount (failed) entry: nothing to categorise
            out.append({"idx": i, "merchant": m, "category": None, "confidence": None,
                        "source": "zero_amount"})
            continue
        r = rule_category(t, m)
        out.append({"idx": i, "merchant": m, "category": r[0] if r else None,
                    "confidence": r[1] if r else None, "source": "rule" if r else None})
        if r is None:
            leftovers.setdefault((m, direction(t)), []).append(i)
    # cache (keyed by merchant; a cached category is only used if it fits the direction)
    cached = cache_get(sorted({m for m, _ in leftovers}))
    for (m, d), idxs in list(leftovers.items()):
        if m in cached and fits(cached[m][0], rows[idxs[0]]):
            for i in idxs:
                out[i].update(category=cached[m][0], confidence=cached[m][1], source="cache")
            del leftovers[(m, d)]
    # LLM, once per unknown merchant
    if leftovers and llm is not None:
        items = [{"merchant": m, "direction": d,
                  "example": rows[idxs[0]].description[:80],
                  "amount": (rows[idxs[0]].debit or rows[idxs[0]].credit) / 100}
                 for (m, d), idxs in leftovers.items()]
        try:
            picks = llm_categories(llm, items)
        except Exception:  # noqa: BLE001 - leave uncategorised; Other is applied below
            picks = {}
        new_cache = []
        for (m, d), idxs in leftovers.items():
            if m in picks:
                cat, conf = picks[m]
                for i in idxs:
                    out[i].update(category=cat, confidence=conf, source="llm")
                new_cache.append((m, cat, conf, "llm"))
        if new_cache:
            cache_put(new_cache)
    for i, a in enumerate(out):  # anything still unknown → Other (low confidence)
        if a["category"] is None and a["source"] != "zero_amount":
            a.update(category="Other Income" if direction(rows[i]) == "credit" else "Other Expense",
                     confidence=0.2, source="default")
    return out
