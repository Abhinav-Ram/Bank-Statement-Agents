"""Deterministic value parsing for statement cells: amounts (→ integer paise), dates, Dr/Cr."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

_CURRENCY = re.compile(r"(?i)^(rs\.?|inr|₹)\s*")
_AMOUNT = re.compile(r"^\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?$|^\d+(?:\.\d{1,2})?$")
_SUFFIX = re.compile(r"(?i)\s*(cr|dr)\.?$")


@dataclass(frozen=True)
class Amount:
    paise: int
    side: str | None = None  # "debit" | "credit" when the text itself says so


# Lookalike characters OCR engines emit inside amounts / Dr-Cr flags.
_LOOKALIKE = str.maketrans({"г": "r", "Г": "r", "С": "C", "с": "c", "О": "0", "о": "0", "D": "D",
                            "Ⅾ": "D", "‚": ",", "，": ",", "．": "."})


def _ocr_normalise(s: str) -> str:
    """Undo common OCR slips in a numeric cell: lookalike letters, 'O'/'o' for 0 between digits,
    a period read for a thousands comma ('48.551.19' → '48,551.19')."""
    s = s.translate(_LOOKALIKE)
    s = re.sub(r"(?<=\d)[Oo](?=[\d.,])|(?<=[\d,])[Oo](?=\d)", "0", s)
    s = re.sub(r"(?<=\d)\s+(?=[.,]\d)|(?<=[.,])\s+(?=\d)", "", s)  # '1,234 .56' → '1,234.56'
    num = re.search(r"\d[\d.,]*\d", s)
    if num and num.group().count(".") > 1:
        g = num.group()
        head, _, tail = g.rpartition(".")
        s = s[: num.start()] + head.replace(".", ",") + "." + tail + s[num.end():]
    return s


def parse_amount(text: str) -> Amount | None:
    s = _ocr_normalise((text or "").strip())
    if not s or s in {"-", "–", "—"}:
        return None
    side = None
    m = _SUFFIX.search(s)
    if m and re.search(r"\d", s[: m.start()]):
        side = "credit" if m.group(1).lower() == "cr" else "debit"
        s = s[: m.start()].strip()
    if s.startswith("(") and s.endswith(")"):
        s, side = s[1:-1].strip(), "debit"
    if s and s[0] in "+-":
        side = "credit" if s[0] == "+" else "debit"
        s = s[1:].strip()
    s = _CURRENCY.sub("", s).strip()
    if s and s[0] in "+-":
        side = "credit" if s[0] == "+" else "debit"
        s = s[1:].strip()
    if not s or not _AMOUNT.match(s):
        return None
    whole, _, frac = s.replace(",", "").partition(".")
    return Amount(int(whole) * 100 + int((frac + "00")[:2]), side)


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_D_NUM = re.compile(r"\b(\d{1,4})[-/.](\d{1,2})[-/.](\d{2,4})\b")
_D_MON = re.compile(r"\b(\d{1,2})[\s\-/]*([A-Za-z]{3,9})\.?[\s\-/,]*(\d{2,4})\b")


def _year(y: str) -> int:
    v = int(y)
    return v + 2000 if v < 100 else v


def parse_date(text: str) -> date | None:
    """Day-first (Indian) dates in the usual statement formats; ISO yyyy-mm-dd also accepted."""
    s = (text or "").strip()
    if not s:
        return None
    try:
        m = _D_NUM.search(s)
        if m:
            a, b, c = m.groups()
            if len(a) == 4:  # ISO
                return date(int(a), int(b), int(c))
            return date(_year(c), int(b), int(a))
        m = _D_MON.search(s)
        if m:
            mon = _MONTHS.get(m.group(2)[:3].lower())
            if mon:
                return date(_year(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None
    return None


def parse_drcr(text: str) -> str | None:
    s = (text or "").strip().lower().rstrip(".")
    if s in {"dr", "d", "debit", "db"}:
        return "debit"
    if s in {"cr", "c", "credit"}:
        return "credit"
    return None


def iso(d: date | datetime | None) -> str | None:
    return d.isoformat() if d else None
