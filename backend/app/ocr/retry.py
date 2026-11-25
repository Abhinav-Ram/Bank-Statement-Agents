"""Scan retry: re-read the rows around a balance break and keep the reading that closes the chain.

One misread balance breaks two rows (i and i+1), so for a break at row i we re-read rows i-1 and i.
Each re-read is an OCR of just that row strip, at 2× with the primary engine and at 1.5× with the
secondary engine, parsed with the page's column layout. Only values some engine actually read
are ever used — nothing is computed into a cell.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from functools import lru_cache

import pymupdf

from app.parsing.layout import Column, RawRow, TableLayout
from app.parsing.rows import to_txn
from app.schemas.statement import Txn

log = logging.getLogger(__name__)
ZOOM = 2.0  # step 1: same engine, 2x
SECOND_UPSCALE = 1.5  # step 2: second engine (only if the two primary readings disagree)


@lru_cache(maxsize=8)
def _page_image(pdf: str, page_no: int):
    from app.ocr.preprocess import preprocess

    return preprocess(pymupdf.open(pdf)[page_no - 1], page_no)


def _layout(layouts: list[dict], page: int) -> TableLayout | None:
    best = None
    for lay in layouts:
        if lay["page"] <= page:
            best = lay
    if best is None:
        return None
    cols = [Column(c["x0"], c["x1"], c["title"], c["role"]) for c in best["columns"]]
    return TableLayout(cols, 0, 0, best["ruled"])


def reread_row(pdf: str, row: Txn, layouts: list[dict], engine: str, upscale: float) -> Txn | None:
    """One alternative reading of an OCR'd row: OCR just its strip, parse with the page layout."""
    from app.ocr.engines import ocr_page

    layout = _layout(layouts, row.page or 1)
    if layout is None or row.bbox is None:
        return None
    img = _page_image(pdf, row.page or 1)
    _x0, y0, _x1, y1 = row.bbox
    pad = 2.5  # points
    region = (0, int((y0 - pad) / img.scale), img.gray.shape[1], int((y1 + pad) / img.scale))
    try:
        words = ocr_page(img, engine, region=region, upscale=upscale)
    except Exception as e:  # noqa: BLE001 - an unavailable engine just yields no reading
        log.info("re-read with %s failed: %s", engine, e)
        return None
    cells: dict[str, list] = {}
    for w in sorted(words, key=lambda w: (round(w.cy), w.x0)):
        col = next((c for c in layout.columns if c.x0 <= w.cx < c.x1), None)
        if col is not None:
            cells.setdefault(col.role, []).append(w.text)
    t = to_txn(RawRow({r: " ".join(v) for r, v in cells.items()}, row.bbox, row.page or 1))
    return t if isinstance(t, Txn) else None


def cascade(prev_balance: int | None, rows: list[Txn],
            read: Callable[[Txn, str], Txn | None]) -> tuple[str, list[Txn], bool]:
    """Resolve a balance break over `rows` (the break row and the one above it).

    step 1  read each row again with the primary engine at 2x ("zoom")
            - a reading that closes the chain → "fixed"
            - every zoom reading equals the original → "verified" (printed that way)
    step 2  only if the primary readings disagree: the second engine ("second")
            - any combination of readings that closes the chain → "fixed"
            - otherwise each row takes its majority reading (≥2 of 3 agree) → "verified"
            - no majority → "unresolved" (human review)
    Returns (outcome, chosen readings, second_engine_used). Never invents a value.
    """
    zoom = [read(r, "zoom") for r in rows]
    best = choose(prev_balance, rows, [[z] if z else [] for z in zoom])
    if best is not None:
        return "fixed", best, False
    if all(z is not None and same_reading(z, r) for z, r in zip(zoom, rows, strict=True)):
        return "verified", list(rows), False

    second = [read(r, "second") for r in rows]
    options = [[x for x in (z, t) if x is not None] for z, t in zip(zoom, second, strict=True)]
    best = choose(prev_balance, rows, options)
    if best is not None:
        return "fixed", best, True
    chosen = []
    for r, z, t in zip(rows, zoom, second, strict=True):
        votes = [x for x in (r, z, t) if x is not None]
        maj = next((v for v in votes if sum(same_reading(v, u) for u in votes) >= 2), None)
        if maj is None:
            return "unresolved", list(rows), True
        chosen.append(maj)
    return "verified", chosen, True


def _net(t: Txn) -> int:
    return t.credit - t.debit


def choose(prev_balance: int | None, rows: list[Txn], cands: list[list[Txn]]) -> list[Txn] | None:
    """Pick one reading per row (original first) so that the chain prev → rows closes, changing
    as few rows as possible. Returns the chosen readings, or None if no combination closes."""
    options = [[r, *c] for r, c in zip(rows, cands, strict=True)]
    best, best_changes = None, None
    for combo in itertools.product(*options):
        if any(t.balance is None for t in combo):
            continue
        prev, ok = prev_balance, True
        for t in combo:
            if prev is not None and prev + _net(t) != t.balance:
                ok = False
                break
            prev = t.balance
        if not ok:
            continue
        changes = sum(c is not r for c, r in zip(combo, rows, strict=True))
        if best is None or changes < best_changes:
            best, best_changes = list(combo), changes
    return best


def same_reading(a: Txn, b: Txn) -> bool:
    return (a.debit, a.credit, a.balance) == (b.debit, b.credit, b.balance)
