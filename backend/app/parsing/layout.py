"""Layout-generic table reconstruction from positioned words.

Input is a page's words (from a PDF text layer now, from OCR word boxes in Phase 1b) plus any
ruling lines. Output is one RawRow per transaction with cell text keyed by semantic role.

  1. header row  = the text line with the most header vocabulary that has a date and a balance
                   column; stacked header words within ±14pt join it ("Transaction / Amount")
  2. columns     = intervals between vertical rules if the table is ruled, else header-word
                   clusters separated at the widest whitespace gap in the body
  3. row anchors = lines whose date-column text parses as a date
  4. row bands   = intervals between horizontal rules if ruled, else nearest anchor
"""

from __future__ import annotations

import itertools
import re
import statistics
from dataclasses import dataclass, field

from app.parsing.values import parse_date

ROLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    (role, re.compile(p, re.IGNORECASE)) for role, p in [
        ("value_date", r"value\s*dt|value\s*date"),
        ("posted", r"posted"),
        ("drcr", r"cr\s*/\s*dr|dr\s*/\s*cr|^type$"),
        ("date", r"\bdate\b|^dt$"),
        ("description", r"description|narration|particulars|details|remarks"),
        ("ref", r"chq|cheque|ref"),
        ("debit", r"debit|withdrawal"),
        ("credit", r"credit|deposit"),
        ("balance", r"balance"),
        ("amount", r"amount|amt"),
    ]
]
HEADER_VOCAB = {
    "date", "txn", "value", "description", "narration", "particulars", "details", "debit",
    "credit", "withdrawal", "withdrawals", "deposit", "deposits", "balance", "amount", "amt",
    "cr/dr", "dr/cr", "chq", "cheque", "ref", "closing", "available", "transaction", "posted",
    "no", "branch", "code", "id", "chq./ref.no.", "remarks", "tran", "type",
}


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Rules:
    vertical: list[tuple[float, float, float]] = field(default_factory=list)  # x, y0, y1
    horizontal: list[tuple[float, float, float]] = field(default_factory=list)  # y, x0, x1


@dataclass
class Column:
    x0: float
    x1: float
    title: str
    role: str


@dataclass
class TableLayout:
    columns: list[Column]
    header_top: float
    header_bottom: float
    ruled_columns: bool


@dataclass
class RawRow:
    cells: dict[str, str]
    bbox: tuple[float, float, float, float]
    page: int


def role_of(title: str) -> str:
    for role, pat in ROLE_PATTERNS:
        if pat.search(title):
            return role
    return "other"


def _norm(t: str) -> str:
    return t.lower().strip(" :.")


def group_lines(words: list[Word], tol: float = 3.0) -> list[list[Word]]:
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.cy, w.x0)):
        if lines and abs(lines[-1][0].cy - w.cy) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(ln, key=lambda w: w.x0) for ln in lines]


def _is_vocab(w: Word) -> bool:
    t = _norm(w.text)
    return t in HEADER_VOCAB or t.rstrip(".") in HEADER_VOCAB


def find_header(words: list[Word]) -> list[Word] | None:
    """The header row = the line (plus words within ±6pt, so a slightly tilted scan's header that
    OCR splits into two lines still counts as one) with the most vocabulary, needing a date and a
    balance/amount title."""
    best, best_score = None, 0
    for line in group_lines(words):
        top, bot = min(w.y0 for w in line) - 6, max(w.y1 for w in line) + 6
        ln = sorted((w for w in words if top <= w.cy <= bot), key=lambda w: w.x0)
        vocab = [w for w in ln if _is_vocab(w)]
        text = " ".join(w.text for w in ln).lower()
        if "date" not in text or not ("balance" in text or "amount" in text):
            continue
        if len(vocab) > best_score:
            best, best_score = ln, len(vocab)
    if best is None or best_score < 3:
        return None
    top = min(w.y0 for w in best) - 14
    bot = max(w.y1 for w in best) + 14
    return [w for w in words if top <= w.cy <= bot and (_is_vocab(w) or w in best)]


def _merge_close(xs: list[float], tol: float = 2.0) -> list[float]:
    out: list[float] = []
    for x in sorted(xs):
        if not out or x - out[-1] > tol:
            out.append(x)
    return out


def _title(ws: list[Word]) -> str:
    return " ".join(w.text for w in sorted(ws, key=lambda w: (round(w.cy), w.x0)))


SUFFIX_WORDS = {"no", "code", "id", "amt", "amount"}


def _header_clusters(header: list[Word], body: list[Word]) -> list[list[Word]]:
    """Group header words into column titles. Stacked words (x-overlap) always join. Side-by-side
    words join when close AND the body has no whitespace channel between them (values run under
    both: 'Deposit Amt.'); a channel means two columns ('Amt. | Closing'), even when OCR boxes
    touch. A Dr/Cr flag is always its own column."""
    clusters: list[list[Word]] = []
    for w in sorted(header, key=lambda w: w.x0):
        if clusters:
            last = clusters[-1]
            prev = max(last, key=lambda c: c.x1)
            stacked = w.x0 < prev.x1 - 1 and any(abs(c.cy - w.cy) > 4 for c in last)
            drcr = role_of(w.text) == "drcr" or role_of(_title(last)) == "drcr"
            close = w.x0 <= prev.x1 + 12
            suffix = _norm(w.text) in SUFFIX_WORDS  # "Cheque No.", "Branch Code", "Withdrawal Amt."
            if stacked or (close and not drcr and (suffix or not _has_channel(prev, w, body))):
                last.append(w)
                continue
        clusters.append([w])
    return clusters


def _has_channel(a: Word, b: Word, body: list[Word], min_width: float = 2.5) -> bool:
    """Is there a vertical strip between a and b that no body word crosses?"""
    lo, hi = a.cx, b.cx
    if hi - lo < min_width:
        return False
    spans = sorted((max(lo, w.x0), min(hi, w.x1)) for w in body if w.x1 > lo and w.x0 < hi)
    if not spans:
        return True
    cur = lo
    for s0, s1 in spans:
        if s0 - cur >= min_width:
            return True
        cur = max(cur, s1)
    return hi - cur >= min_width


def _transaction_lines(body: list[Word]) -> list[Word]:
    """Words on lines that start with a date (fallback: everything)."""
    out = []
    for ln in group_lines(body):
        if any(parse_date(" ".join(w.text for w in ln[k:k + 3])) for k in range(min(2, len(ln)))):
            out += ln
    return out or body


def _vertical_borders(rules: Rules, h_top: float, h_mid: float, h_bot: float) -> list[float]:
    """x of column borders: rules crossing the header, or under it with enough total length
    (scan borders are often broken into per-row segments, some of them lost)."""
    crossing = [x for x, y0, y1 in rules.vertical if y0 - 2 <= h_mid <= y1 + 2]
    below: dict[float, float] = {}
    for x, y0, y1 in rules.vertical:
        if y0 >= h_top - 5:
            key = next((k for k in below if abs(k - x) <= 2), x)
            below[key] = below.get(key, 0) + (y1 - y0)
    return _merge_close(crossing + [x for x, length in below.items() if length >= 40])


def build_layout(header: list[Word], body: list[Word], rules: Rules) -> TableLayout:
    h_top = min(w.y0 for w in header)
    h_bot = max(w.y1 for w in header)
    h_mid = (h_top + h_bot) / 2
    body = _transaction_lines(body)  # summaries / footers must not hide column gaps
    clusters = _header_clusters(header, body)
    spans = [(min(w.x0 for w in c), max(w.x1 for w in c), _title(c)) for c in clusters]

    vx = _vertical_borders(rules, h_top, h_mid, h_bot)
    if len(vx) >= 4:
        cols: list[Column] = []
        for a, b in itertools.pairwise(vx):
            inside = [sp for sp in spans if a <= (sp[0] + sp[1]) / 2 <= b]
            if not inside:
                continue
            if len({role_of(t) for _, _, t in inside} - {"other"}) < 2:
                t = " ".join(sp[2] for sp in inside)
                cols.append(Column(a, b, t, role_of(t)))
                continue
            # A missed border leaves two titled columns in one cell: split them at the gap.
            edges = [a, *[_split_between(p, q, body) for p, q in itertools.pairwise(inside)], b]
            cols += [Column(edges[i], edges[i + 1], t, role_of(t))
                     for i, (_, _, t) in enumerate(inside)]
        if len(cols) >= 3:
            return TableLayout(cols, h_top, h_bot, True)

    bounds = [_split_between(a, b, body) for a, b in itertools.pairwise(spans)]
    edges = [-1e9, *bounds, 1e9]
    cols = [Column(edges[i], edges[i + 1], t, role_of(t)) for i, (_, _, t) in enumerate(spans)]
    return TableLayout(cols, h_top, h_bot, False)


def _split_between(a: tuple, b: tuple, body: list[Word]) -> float:
    """Boundary between header spans a and b: centre of the widest body whitespace gap."""
    lo, hi = (a[0] + a[1]) / 2, (b[0] + b[1]) / 2
    step = 0.5
    n = max(1, int((hi - lo) / step))
    cover = [0] * (n + 1)
    for w in body:
        if w.x1 < lo or w.x0 > hi:
            continue
        i0, i1 = max(0, int((w.x0 - lo) / step)), min(n, int((w.x1 - lo) / step))
        for i in range(i0, i1 + 1):
            cover[i] += 1
    floor = min(cover)
    best_len, best_mid, run_start = -1, (lo + hi) / 2, None
    for i, c in enumerate([*cover, floor + 1]):
        if c == floor and run_start is None:
            run_start = i
        elif c != floor and run_start is not None:
            if i - run_start > best_len:
                best_len, best_mid = i - run_start, lo + step * (run_start + i - 1) / 2
            run_start = None
    return best_mid


def date_column(layout: TableLayout) -> Column:
    for role in ("date", "posted", "value_date"):
        for c in layout.columns:
            if c.role == role:
                return c
    return layout.columns[0]


def column_of(w: Word, layout: TableLayout) -> Column | None:
    for c in layout.columns:
        if c.x0 <= w.cx < c.x1:
            return c
    return None


def extract_rows(words: list[Word], rules: Rules, layout: TableLayout, page: int,
                 zone_top: float) -> tuple[list[RawRow], float]:
    """Rows on one page below `zone_top`. Returns rows and the y where the table ends."""
    body = [w for w in words if w.cy > zone_top]
    dc = date_column(layout)

    lines = group_lines(body)
    anchors: list[tuple[float, float]] = []  # (top, centre) of each row's date line
    for ln in lines:
        cell = [w for w in ln if column_of(w, layout) is dc]
        if cell and parse_date(" ".join(w.text for w in cell[:3])):
            anchors.append((min(w.y0 for w in cell), sum(w.cy for w in cell) / len(cell)))
    if not anchors:
        return [], zone_top
    cys = [c for _, c in anchors]
    pitch = statistics.median([b - a for a, b in itertools.pairwise(cys)]) if len(cys) > 1 else 14.0

    table_x0 = min(c.x0 for c in layout.columns if c.x0 > -1e8) if layout.ruled_columns else -1e9
    table_x1 = max(c.x1 for c in layout.columns if c.x1 < 1e8) if layout.ruled_columns else 1e9
    width = (table_x1 - table_x0) if layout.ruled_columns else None
    hy = sorted(y for y, x0, x1 in rules.horizontal
                if y > zone_top and (width is None or x1 - x0 >= 0.4 * width))
    hy = _merge_close(hy, 1.5)
    text_roles = {"description", "ref", "other"}

    # Row extents.
    extents: list[tuple[float, float]] = []
    use_bands = len(hy) >= 2
    for k, (ay0, acy) in enumerate(anchors):
        nxt = anchors[k + 1] if k + 1 < len(anchors) else None
        if use_bands:
            top = max([y for y in hy if y <= acy] or [zone_top])
            bot_c = [y for y in hy if y > acy]
            bot = bot_c[0] if bot_c else None
            if nxt is not None and bot is not None and nxt[1] < bot:  # two anchors, one band
                bot = nxt[0] - 0.5
            if k > 0 and extents[-1][1] > top:  # previous anchor shares this band
                top = extents[-1][1]
        else:
            # Unruled: cells are top-aligned, so continuation lines (no date) belong to the
            # row above; a row runs from its date line down to the next date line.
            top = extents[-1][1] if extents else ay0 - 0.4 * pitch
            bot = nxt[0] - 0.5 if nxt is not None else None
        if bot is None:  # last row on the page with nothing below it to bound it
            bot, last_cy = acy + 0.5, acy
            for ln in lines:  # take wrapped text-only lines directly underneath
                lc = sum(w.cy for w in ln) / len(ln)
                if lc <= last_cy:
                    continue
                roles = {getattr(column_of(w, layout), "role", None) for w in ln}
                if lc - last_cy > 1.3 * min(pitch, 14.0) or not roles <= text_roles:
                    break
                bot, last_cy = lc + 0.5, lc
        extents.append((top, bot))

    rows: list[RawRow] = []
    for top, bot in extents:
        ws = [w for w in body if top <= w.cy < bot and table_x0 - 2 <= w.cx <= table_x1 + 2]
        cells: dict[str, list[Word]] = {}
        for w in ws:
            c = column_of(w, layout)
            if c is not None:
                cells.setdefault(c.role, []).append(w)
        text = {r: " ".join(w.text for ln in group_lines(v) for w in ln) for r, v in cells.items()}
        if ws:
            bbox = (min(w.x0 for w in ws), min(w.y0 for w in ws),
                    max(w.x1 for w in ws), max(w.y1 for w in ws))
            rows.append(RawRow(text, bbox, page))
    return rows, extents[-1][1]
