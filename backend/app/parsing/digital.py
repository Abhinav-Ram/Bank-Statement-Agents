"""PDF → transactions with no LLM: per-page detect, then text layer (digital) or OCR (scanned)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.parsing.layout import (
    Rules,
    TableLayout,
    Word,
    build_layout,
    extract_rows,
    find_header,
    group_lines,
)
from app.parsing.rows import to_txn
from app.schemas.statement import Txn


@dataclass
class ParseResult:
    rows: list[Txn]
    n_pages: int
    opening_marker: int | None = None  # balance on a "B/F" row, if printed
    head_text: str = ""  # page-1 text above the table (metadata lives here)
    tail_text: str = ""  # text after the last row (summaries live here)
    pages_without_table: list[int] = field(default_factory=list)
    has_text_layer: bool = True
    layouts: list[dict] = field(default_factory=list)
    page_kinds: list[str] = field(default_factory=list)  # "digital" | "scanned" per page
    scanned_pages: list[int] = field(default_factory=list)  # image pages not OCR'd (no engine)
    source_type: str = "digital"  # digital | scanned | mixed
    skew: list[float] = field(default_factory=list)


def page_words(page: pymupdf.Page) -> list[Word]:
    """Horizontal words only: diagonal watermarks ('SYNTHETIC DATA ...') are dropped."""
    tp = page.get_textpage()
    skew = set()
    for b in page.get_text("dict", textpage=tp)["blocks"]:
        for li, ln in enumerate(b.get("lines", [])):
            dx, dy = ln["dir"]
            if abs(dy) > 0.05 or dx < 0:
                skew.add((b["number"], li))
    return [Word(x0, y0, x1, y1, t) for x0, y0, x1, y1, t, bn, ln, _ in
            page.get_text("words", textpage=tp) if (bn, ln) not in skew and t.strip()]


def page_rules(page: pymupdf.Page) -> Rules:
    r = Rules()

    def seg(a: pymupdf.Point, b: pymupdf.Point) -> None:
        if abs(a.x - b.x) < 0.8 and abs(a.y - b.y) > 4:
            r.vertical.append((a.x, min(a.y, b.y), max(a.y, b.y)))
        elif abs(a.y - b.y) < 0.8 and abs(a.x - b.x) > 4:
            r.horizontal.append((a.y, min(a.x, b.x), max(a.x, b.x)))

    for d in page.get_drawings():
        stroked = d.get("color") is not None
        for it in d["items"]:
            if it[0] == "l":
                seg(it[1], it[2])
            elif it[0] == "re":
                q = it[1]
                if q.width < 1.5 and q.height > 4:
                    r.vertical.append(((q.x0 + q.x1) / 2, q.y0, q.y1))
                elif q.height < 1.5 and q.width > 4:
                    r.horizontal.append(((q.y0 + q.y1) / 2, q.x0, q.x1))
                elif stroked:
                    for a, b in ((q.tl, q.tr), (q.bl, q.br), (q.tl, q.bl), (q.tr, q.br)):
                        seg(a, b)
    return r


def _text(words: list[Word]) -> str:
    return "\n".join(" ".join(w.text for w in ln) for ln in group_lines(words))


MIN_TEXT_CHARS = 50  # fewer characters than this on a page → treat it as a scanned image


def parse_pdf(path: str | Path, ocr_engine: str | None = None) -> ParseResult:
    """detect → parse, per page. Pages with a text layer use it directly; image-only pages are
    preprocessed and OCR'd with `ocr_engine` (or reported in `scanned_pages` if None). Both paths
    produce positioned words + ruling lines and share the same table reconstruction."""
    doc = pymupdf.open(path)
    res = ParseResult(rows=[], n_pages=doc.page_count)
    layout: TableLayout | None = None
    tail_words: list[Word] = []
    for pno, page in enumerate(doc, start=1):
        words = page_words(page)
        if sum(len(w.text) for w in words) >= MIN_TEXT_CHARS:
            kind, rules = "digital", page_rules(page)
            size = (round(page.rect.width, 1), round(page.rect.height, 1))
        elif ocr_engine is None:
            res.page_kinds.append("scanned")
            res.scanned_pages.append(pno)
            continue
        else:
            from app.ocr.engines import ocr_page
            from app.ocr.preprocess import detect_rules, preprocess

            img = preprocess(page, pno)
            kind, rules, words = "scanned", detect_rules(img), ocr_page(img, ocr_engine)
            h_px, w_px = img.gray.shape
            size = (round(w_px * img.scale, 1), round(h_px * img.scale, 1))
            res.skew.append(img.angle)
        res.page_kinds.append(kind)
        header = find_header(words)
        if header:
            body = [w for w in words if w.cy > max(h.y1 for h in header)]
            layout = build_layout(header, body, rules)
            zone_top = layout.header_bottom
            if pno == 1:
                res.head_text = _text([w for w in words if w.cy < layout.header_top])
        elif layout is None:
            if pno == 1:
                res.head_text = _text(words)
            res.pages_without_table.append(pno)
            continue
        else:
            zone_top = 0.0  # continuation page: reuse the previous page's columns
        res.layouts.append({"page": pno, "kind": kind, "ruled": layout.ruled_columns,
                            "width": size[0], "height": size[1],  # bbox coordinate space
                            "columns": [{"x0": c.x0, "x1": c.x1, "title": c.title,
                                         "role": c.role} for c in layout.columns]})
        raw, end_y = extract_rows(words, rules, layout, pno, zone_top)
        if not raw:
            res.pages_without_table.append(pno)
        for rr in raw:
            t = to_txn(rr)
            if isinstance(t, int):
                if not res.rows and res.opening_marker is None:
                    res.opening_marker = t
            else:
                if kind == "scanned":
                    t.source = "ocr"
                res.rows.append(t)
        if raw:
            tail_words = [w for w in words if w.cy > end_y]
    resolve_zero_amount_rows(res.rows, res.opening_marker)
    res.tail_text = _text(tail_words)
    res.has_text_layer = not res.scanned_pages and "scanned" not in res.page_kinds
    kinds = set(res.page_kinds)
    res.source_type = "mixed" if len(kinds) > 1 else (kinds.pop() if kinds else "digital")
    return res


def resolve_zero_amount_rows(rows: list[Txn], opening: int | None) -> None:
    """A row printed without any amount whose balance is unchanged (e.g. 'FAILED-INSUFFICIENT
    FUNDS-...') is a zero-amount entry, not an extraction problem."""
    prev = opening
    for t in rows:
        if ("no_amount" in t.ambiguous and t.balance is not None
                and (prev is None or prev == t.balance)):
            t.ambiguous.remove("no_amount")
            t.notes.append("zero_amount")
        prev = t.balance if t.balance is not None else prev
