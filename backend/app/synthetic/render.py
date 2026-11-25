"""Render synthetic statements to PDF in three distinct (fictional) bank layouts,
and optionally degrade them into image-only "scanned" PDFs.

Layouts deliberately differ in the ways real statements do:
  northwind : separate Withdrawal/Deposit columns, dd/mm/yy, summary block at the END
  coral     : single Amount column + Dr/Cr flag, dd-Mon-yyyy, "B/F" opening row,
              long narrations WRAP onto extra lines, balances suffixed "Cr"
  summit    : landscape, Txn Date + Value Date, dd.mm.yyyy, summary at the TOP,
              header repeated on every page, "Page x of y" footers
"""

from __future__ import annotations

import io
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from app.synthetic.generator import Statement, Txn

BANKS = {
    "northwind": "Northwind Bank",
    "coral": "Coral Savings Bank",
    "summit": "Summit Co-operative Bank",
}


def inr(paise: int) -> str:
    """Indian digit grouping: 1234567.5 -> 12,34,567.50"""
    neg = paise < 0
    rupees, p = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{'-' if neg else ''}{s}.{p:02d}"


@dataclass
class Col:
    title: str
    x: float
    width: float
    align: str  # "l" | "r"
    get: Callable[[Txn], str]
    wrap: bool = False


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    words, lines, cur = text.replace("/", "/ ").split(" "), [], ""
    for w in words:
        trial = (cur + " " + w).strip() if cur and not cur.endswith("/") else cur + w
        if stringWidth(trial.replace("/ ", "/"), font, size) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return [ln.replace("/ ", "/").strip() for ln in lines] or [""]


class _Doc:
    def __init__(self, stmt: Statement, pagesize, font_size: float = 7.5):
        self.stmt = stmt
        self.buf = io.BytesIO()
        self.c = Canvas(self.buf, pagesize=pagesize)
        self.c.setTitle(f"{stmt.bank} statement")
        self.W, self.H = pagesize
        self.fs = font_size
        self.page = 1

    def text(self, x, y, s, size=None, bold=False, align="l"):
        font = "Helvetica-Bold" if bold else "Helvetica"
        self.c.setFont(font, size or self.fs)
        if align == "r":
            self.c.drawRightString(x, y, s)
        elif align == "c":
            self.c.drawCentredString(x, y, s)
        else:
            self.c.drawString(x, y, s)

    def table(self, cols: list[Col], rows: list[Txn], y_top: float, y_bottom: float,
              header_fn: Callable[[], float], footer_fn: Callable[[], None] | None = None,
              pre_rows: list[list[str]] | None = None, grid: bool = False, row_h: float = 11.5):
        """Draw rows, breaking pages as needed. Records the page number on each Txn."""

        def header(y):
            if grid:
                self.c.setFillGray(0.88)
                self.c.rect(cols[0].x - 3, y - 4, cols[-1].x + cols[-1].width - cols[0].x + 6, row_h + 2,
                            stroke=0, fill=1)
                self.c.setFillGray(0)
            for col in cols:
                xx = col.x + col.width if col.align == "r" else col.x
                self.text(xx, y, col.title, bold=True, align=col.align)
            return y - row_h - 3

        y = header(y_top)
        for cells in pre_rows or []:
            for col, val in zip(cols, cells):
                xx = col.x + col.width if col.align == "r" else col.x
                self.text(xx, y, val, align=col.align)
            y -= row_h

        for t in rows:
            cells = []
            n_lines = 1
            for col in cols:
                val = col.get(t)
                lines = _wrap(val, "Helvetica", self.fs, col.width) if col.wrap else [val]
                n_lines = max(n_lines, len(lines))
                cells.append(lines)
            needed = row_h + (n_lines - 1) * (self.fs + 1.5)
            if y - needed < y_bottom:
                if footer_fn:
                    footer_fn()
                self.c.showPage()
                self.page += 1
                y = header(header_fn())
            t.page = self.page
            for col, lines in zip(cols, cells):
                xx = col.x + col.width if col.align == "r" else col.x
                for k, ln in enumerate(lines):
                    self.text(xx, y - k * (self.fs + 1.5), ln, align=col.align)
            if grid:
                self.c.setStrokeGray(0.8)
                self.c.line(cols[0].x - 3, y - needed + row_h - 4, cols[-1].x + cols[-1].width + 3,
                            y - needed + row_h - 4)
            y -= needed
        return y

    def finish(self) -> bytes:
        self.c.showPage()
        self.c.save()
        return self.buf.getvalue()


def _d(t: Txn, fmt: str) -> str:
    return date.fromisoformat(t.date).strftime(fmt)


# ---------------------------------------------------------------- layouts
def render_northwind(s: Statement) -> bytes:
    doc = _Doc(s, A4)
    W, H = doc.W, doc.H

    def page_top() -> float:
        doc.text(40, H - 40, "NORTHWIND BANK", size=14, bold=True)
        doc.text(W - 40, H - 40, f"Statement of Account  |  Page {doc.page}", align="r")
        return H - 70

    page_top()
    y = H - 62
    for line in [f"Account Holder : {s.holder}", f"Account No     : {s.account_masked}",
                 f"IFSC           : {s.ifsc}     Branch: {s.branch}",
                 f"Period         : {_fmt(s.period_start, '%d/%m/%Y')} to {_fmt(s.period_end, '%d/%m/%Y')}"]:
        doc.text(40, y, line, size=8.5)
        y -= 12
    cols = [
        Col("Date", 40, 42, "l", lambda t: _d(t, "%d/%m/%y")),
        Col("Narration", 86, 215, "l", lambda t: t.description, wrap=True),
        Col("Chq./Ref.No.", 304, 62, "l", lambda t: t.ref),
        Col("Withdrawal Amt.", 368, 62, "r", lambda t: inr(t.debit) if t.debit else ""),
        Col("Deposit Amt.", 434, 58, "r", lambda t: inr(t.credit) if t.credit else ""),
        Col("Closing Balance", 496, 62, "r", lambda t: inr(t.balance)),
    ]
    y = doc.table(cols, s.transactions, y - 14, 70, page_top)
    if y < 140:
        doc.c.showPage()
        doc.page += 1
        y = page_top()
    td, tc = sum(t.debit for t in s.transactions), sum(t.credit for t in s.transactions)
    y -= 16
    doc.text(40, y, "STATEMENT SUMMARY", bold=True, size=9)
    y -= 14
    heads = ["Opening Balance", "Dr Count", "Cr Count", "Debits", "Credits", "Closing Bal"]
    vals = [inr(s.opening_balance), str(sum(1 for t in s.transactions if t.debit)),
            str(sum(1 for t in s.transactions if t.credit)), inr(td), inr(tc), inr(s.closing_balance)]
    for i, (h, v) in enumerate(zip(heads, vals)):
        doc.text(40 + i * 88, y, h, bold=True)
        doc.text(40 + i * 88, y - 12, v)
    doc.text(40, 40, "This is a computer generated statement and does not require a signature.",
             size=6.5)
    return doc.finish()


def render_coral(s: Statement) -> bytes:
    doc = _Doc(s, A4, font_size=8)
    W, H = doc.W, doc.H

    def page_top() -> float:
        doc.text(W / 2, H - 38, "Coral Savings Bank Ltd.", size=13, bold=True, align="c")
        doc.text(W / 2, H - 52, f"{s.branch} Branch  -  IFSC {s.ifsc}", size=8, align="c")
        return H - 80

    page_top()
    doc.text(50, H - 76, f"Customer: {s.holder}", size=9)
    doc.text(50, H - 89, f"A/c No: {s.account_masked}   Type: SAVINGS", size=9)
    doc.text(W - 50, H - 76, f"From: {_fmt(s.period_start, '%d-%b-%Y')}", size=9, align="r")
    doc.text(W - 50, H - 89, f"To:   {_fmt(s.period_end, '%d-%b-%Y')}", size=9, align="r")
    cols = [
        Col("Date", 50, 55, "l", lambda t: _d(t, "%d-%b-%Y")),
        Col("Description", 110, 215, "l", lambda t: t.description, wrap=True),
        Col("Amount", 330, 75, "r", lambda t: inr(t.debit or t.credit)),
        Col("Dr/Cr", 415, 25, "l", lambda t: "Dr" if t.debit else "Cr"),
        Col("Balance", 445, 100, "r", lambda t: _crdr(t.balance)),
    ]
    bf = [_fmt(s.period_start, "%d-%b-%Y"), "B/F", "", "", _crdr(s.opening_balance)]
    y = doc.table(cols, s.transactions, H - 112, 60, page_top, pre_rows=[bf], grid=True, row_h=12)
    doc.text(110, y - 4, "C/F", bold=True)
    doc.text(545, y - 4, _crdr(s.closing_balance), bold=True, align="r")
    return doc.finish()


def render_summit(s: Statement, total_pages: int | None = None) -> bytes:
    if total_pages is None:  # two-pass: count pages, then render with "Page x of N"
        import pymupdf

        n = pymupdf.open(stream=render_summit(s, total_pages=0), filetype="pdf").page_count
        return render_summit(s, total_pages=n)
    doc = _Doc(s, landscape(A4), font_size=7.5)
    W, H = doc.W, doc.H

    def page_top() -> float:
        doc.text(36, H - 34, "SUMMIT CO-OPERATIVE BANK", size=12, bold=True)
        doc.text(W - 36, H - 34, f"A/c {s.account_masked}", align="r", size=9)
        return H - 60

    def footer():
        doc.text(W / 2, 22, f"Page {doc.page} of {total_pages}", align="c", size=7)

    page_top()
    td, tc = sum(t.debit for t in s.transactions), sum(t.credit for t in s.transactions)
    y = H - 54
    meta = [("Name", s.holder), ("Branch", s.branch), ("IFSC", s.ifsc),
            ("Statement Period", f"{_fmt(s.period_start, '%d.%m.%Y')} - {_fmt(s.period_end, '%d.%m.%Y')}")]
    for k, v in meta:
        doc.text(36, y, f"{k}:", bold=True, size=8)
        doc.text(130, y, v, size=8)
        y -= 11
    summ = [("Opening Balance", inr(s.opening_balance)), ("Total Debits", inr(td)),
            ("Total Credits", inr(tc)), ("Closing Balance", inr(s.closing_balance))]
    yy = H - 54
    for k, v in summ:
        doc.text(560, yy, k, bold=True, size=8)
        doc.text(W - 36, yy, v, size=8, align="r")
        yy -= 11
    cols = [
        Col("Txn Date", 36, 50, "l", lambda t: _d(t, "%d.%m.%Y")),
        Col("Value Date", 92, 50, "l", lambda t: date.fromisoformat(t.value_date).strftime("%d.%m.%Y")),
        Col("Description", 150, 330, "l", lambda t: t.description),
        Col("Debit", 490, 85, "r", lambda t: inr(t.debit) if t.debit else "-"),
        Col("Credit", 585, 85, "r", lambda t: inr(t.credit) if t.credit else "-"),
        Col("Balance", 680, 120, "r", lambda t: inr(t.balance)),
    ]
    doc.table(cols, s.transactions, y - 14, 44, page_top, footer_fn=footer, grid=True)
    footer()
    return doc.finish()


RENDERERS = {"northwind": render_northwind, "coral": render_coral, "summit": render_summit}


def render(stmt: Statement) -> bytes:
    return RENDERERS[stmt.layout](stmt)


# ---------------------------------------------------------------- scan simulation
def simulate_scan(pdf_bytes: bytes, seed: int, dpi: int = 150, quality: str = "medium") -> bytes:
    """Rasterize + degrade (tilt, blur, noise, uneven lighting, JPEG) into an image-only PDF."""
    import numpy as np
    import pymupdf as fitz
    from PIL import Image, ImageFilter

    rng = random.Random(seed)
    nprng = np.random.default_rng(seed)
    lvl = {"good": (0.4, 0.3, 4), "medium": (1.2, 0.6, 9), "poor": (2.2, 1.0, 16)}[quality]
    max_tilt, blur, noise_sd = lvl

    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        img = img.rotate(rng.uniform(-max_tilt, max_tilt), resample=Image.BICUBIC, expand=False,
                         fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(blur))
        arr = np.asarray(img, dtype=np.float32)
        h, w = arr.shape
        grad = np.linspace(rng.uniform(-18, 0), rng.uniform(0, 12), w)[None, :]  # uneven lighting
        arr = arr * rng.uniform(0.86, 0.97) + 255 * 0.04 + grad + nprng.normal(0, noise_sd, (h, w))
        img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
        jb = io.BytesIO()
        img.save(jb, format="JPEG", quality=rng.randint(55, 80))
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=jb.getvalue())
    return out.tobytes(garbage=3, deflate=True)


def _crdr(paise: int) -> str:
    """Balance as banks print it: magnitude + Cr (in credit) / Dr (overdrawn)."""
    return f"{inr(abs(paise))} {'Dr' if paise < 0 else 'Cr'}"


def _fmt(iso: str, fmt: str) -> str:
    return date.fromisoformat(iso).strftime(fmt)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
