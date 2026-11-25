"""Make statements that need human review, with a known answer key (for user-led testing).

    uv run python scripts/make_review_cases.py      # → data/review_cases/

Each case is a synthetic statement (exact arithmetic) with one deliberate fault:
  amount_typo     one debit amount printed with two digits swapped
  balance_typo    one running balance printed with two digits swapped
  closing_misread metadata closing balance misread (PDF untouched; the sidecar meta is wrong)
The PDFs are edited in place on the page (old number blanked, new one drawn right-aligned in the
same font size), so the reviewer sees the wrong number when they open the PDF.
<case>.meta.json holds the statement metadata the review run uses instead of a Groq call.
ANSWERS.md says what was changed — read it after reviewing, not before.
"""

from __future__ import annotations

import json
import shutil
import sys
from itertools import pairwise
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.datasets.truth import iter_dataset
from app.parsing.digital import parse_pdf
from app.services.review import inr


def swap_digits(text: str) -> str:
    """'2,524.29' → '2,254.29': swap the first adjacent pair of different digits after the
    leading digit, so the result is a plausible typo of the same length."""
    chars = list(text)
    pos = [i for i, c in enumerate(chars) if c.isdigit()]
    for a, b in pairwise(pos[1:]):
        if b == a + 1 and chars[a] != chars[b]:
            chars[a], chars[b] = chars[b], chars[a]
            return "".join(chars)
    raise ValueError(f"no swappable digits in {text!r}")


def replace_cell(pdf_in: Path, pdf_out: Path, page_no: int, bbox, old: str, new: str) -> None:
    doc = pymupdf.open(pdf_in)
    page = doc[page_no - 1]
    y0, y1 = bbox[1], bbox[3]
    hits = [s for b in page.get_text("dict")["blocks"] for line in b.get("lines", [])
            for s in line["spans"] if s["text"].strip() == old
            and y0 - 2 <= (s["bbox"][1] + s["bbox"][3]) / 2 <= y1 + 2]
    if not hits:
        raise ValueError(f"{old!r} not found on page {page_no} in row band {y0:.0f}-{y1:.0f}")
    span = hits[0]
    r = pymupdf.Rect(span["bbox"])
    page.add_redact_annot(r, fill=(1, 1, 1))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
    size = span["size"]
    width = pymupdf.get_text_length(new, fontname="helv", fontsize=size)
    page.insert_text((r.x1 - width, span["origin"][1]), new, fontname="helv", fontsize=size)
    doc.save(pdf_out)


def main() -> None:
    s = get_settings()
    out = s.data_dir / "review_cases"
    out.mkdir(parents=True, exist_ok=True)
    syn = {t.id: t for t in iter_dataset("synthetic", s.data_dir)}
    answers = ["# Review cases — answer key", "",
               "Read after reviewing. Row numbers are 1-based, as the review shows them.", ""]

    # 1) amount typo (Summit layout: separate Debit / Credit columns)
    tr = syn["summit_01"]
    rows = parse_pdf(tr.pdf).rows
    i = next(k for k in range(8, len(rows)) if rows[k].debit and rows[k].page == 1)
    old = rows[i].raw["debit"]
    new = swap_digits(old)
    replace_cell(tr.pdf, out / "amount_typo.pdf", rows[i].page, rows[i].bbox, old, new)
    (out / "amount_typo.meta.json").write_text(json.dumps(tr.meta.model_dump(), indent=1))
    answers += ["## amount_typo.pdf", (f"Row {i + 1} (page {rows[i].page}, {rows[i].date}, "
                f"{rows[i].description[:40]}): debit printed **{new}**, true value **{old}**. "
                "Expected: a 'fix' card for that row (likely a typo on the statement) whose first "
                "suggestion is the true amount; "
                "a follow-on totals card marked 'check'."), ""]

    # 2) balance typo (Northwind layout: one amount column + Dr/Cr)
    tr = syn["northwind_01"]
    rows = parse_pdf(tr.pdf).rows
    i = next(k for k in range(10, len(rows) - 1) if rows[k].page == 1
             and rows[k + 1].page == 1 and rows[k].balance and rows[k].balance > 0)
    old = rows[i].raw["balance"].split()[0]
    new = swap_digits(old)
    replace_cell(tr.pdf, out / "balance_typo.pdf", rows[i].page, rows[i].bbox, old, new)
    (out / "balance_typo.meta.json").write_text(json.dumps(tr.meta.model_dump(), indent=1))
    answers += ["## balance_typo.pdf", (f"Row {i + 1} (page {rows[i].page}, {rows[i].date}): "
                f"balance printed **{new}**, true value **{old}**. Expected: cards for rows "
                f"{i + 1} and {i + 2}; on row {i + 1} the likely suggestion is the true balance "
                f"and says it also makes row {i + 2} add up; row {i + 2}'s card says it follows "
                f"from row {i + 1} (confirm only)."), ""]

    # 3) closing balance misread (metadata only; PDF is the original)
    tr = syn["coral_02"]
    shutil.copy(tr.pdf, out / "closing_misread.pdf")
    meta = tr.meta.model_dump()
    true_close = meta["closing_balance"]
    meta["closing_balance"] = true_close + 9_000_00
    (out / "closing_misread.meta.json").write_text(json.dumps(meta, indent=1))
    answers += ["## closing_misread.pdf", (f"The PDF is correct. The metadata says the closing "
                f"balance is {inr(meta['closing_balance'])}; the true closing balance is "
                f"{inr(true_close)} (as printed, and equal to the last row). Expected: one "
                "'fix' card suggesting the true closing balance; no row cards."), ""]

    answers += ["## AgamiAI statements (run with --dataset)",
                ("These print running balances that don't add up in ~12% of rows (a dataset "
                "property). Expected: cards saying the PDF itself prints these numbers (some marked "
                "'likely wrong' = probably a typo in the statement). Open the page, confirm the "
                "printed values match, and choose Keep as printed (K keeps all): the dataset's "
                "ground truth has the same numbers."), ""]
    (out / "ANSWERS.md").write_text("\n".join(answers))
    print(f"wrote {len(list(out.glob('*.pdf')))} cases → {out}")


if __name__ == "__main__":
    main()
