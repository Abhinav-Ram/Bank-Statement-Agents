"""Scanned-path guardrails: deskew, ruling lines, detect routing, coordinate normalisation."""

import cv2
import numpy as np
import pymupdf
import pytest

from app.ocr.preprocess import PageImage, detect_rules, estimate_skew, rotate


def text_like_page(w=1240, h=1754) -> np.ndarray:
    img = np.full((h, w), 255, np.uint8)
    for k, y in enumerate(range(150, h - 150, 40)):
        cv2.putText(img, f"01/01/2024 UPI/{k:04d}/MERCHANT NAME 1,234.56 98,765.43",
                    (80, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2)
    return img


@pytest.mark.parametrize("angle", [-2.0, -0.7, 0.0, 1.3, 2.5])
def test_deskew_recovers_rotation(angle):
    img = rotate(text_like_page(), angle)
    est = estimate_skew(img)
    assert abs(est + angle) <= 0.3  # correction is the opposite rotation


def test_rules_detected_in_normalised_points():
    w, h = 2480, 3508
    img = np.full((h, w), 255, np.uint8)
    for y in (1000, 1100, 1200):
        cv2.line(img, (200, y), (2280, y), 0, 3)
    for x in (200, 900, 2280):
        cv2.line(img, (x, 1000), (x, 1200), 0, 3)
    page = PageImage(img, 595 / w, 0.0, 1)
    r = detect_rules(page)
    ys = sorted(round(y) for y, _, _ in r.horizontal)
    xs = sorted(round(x) for x, _, _ in r.vertical)
    assert ys == [round(v * 595 / w) for v in (1000, 1100, 1200)]
    assert len(xs) == 3 and abs(xs[0] - 48) <= 1 and abs(xs[-1] - 547) <= 1


def test_detect_routes_image_pages_without_engine(tmp_path):
    from app.parsing.digital import parse_pdf

    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Date Description Debit Credit Balance " * 3)
    p2 = doc.new_page()
    _ok, png = cv2.imencode(".png", text_like_page())
    p2.insert_image(p2.rect, stream=png.tobytes())
    f = tmp_path / "mixed.pdf"
    doc.save(f)
    res = parse_pdf(f)  # no OCR engine → image page is reported, not guessed at
    assert res.page_kinds == ["digital", "scanned"] and res.scanned_pages == [2]
    assert not res.has_text_layer


# ------------------------------------------------------------------ retry candidate selection
from app.ocr.retry import choose
from app.schemas.statement import Txn


def T(debit=0, credit=0, bal=0):
    return Txn(date="2024-01-01", debit=debit, credit=credit, balance=bal, source="ocr")


def test_choose_fixes_misread_balance_using_another_reading():
    # true: 1000 → -200 → 800 → +50 → 850 ; OCR misread row 0's balance as 300 (8→3)
    rows = [T(debit=200, bal=300), T(credit=50, bal=850)]
    cands = [[T(debit=200, bal=800)], [T(credit=50, bal=850)]]
    got = choose(1000, rows, cands)
    assert [t.balance for t in got] == [800, 850]
    assert got[1] is rows[1]  # unchanged row keeps the original reading


def test_choose_returns_none_when_no_reading_closes():
    rows = [T(debit=200, bal=300)]
    assert choose(1000, rows, [[T(debit=200, bal=301)]]) is None


def test_choose_prefers_fewest_changes():
    rows = [T(debit=200, bal=800)]
    assert choose(1000, rows, [[T(debit=200, bal=800)]])[0] is rows[0]


@pytest.mark.parametrize("lean", [-1.5, -0.8, 0.9, 1.6])
def test_unshear_makes_leaning_borders_vertical(lean):
    from app.ocr.preprocess import unshear, vertical_tilt

    h, w = 3000, 2000
    img = np.full((h, w), 255, np.uint8)
    k = np.tan(np.radians(lean))
    for x in range(200, 1900, 150):  # table borders leaning by `lean` degrees
        cv2.line(img, (int(x - k * h / 2), 0), (int(x + k * h / 2), h - 1), 0, 3)
    for y in range(300, 2800, 120):  # rows stay level (a shear, not a rotation)
        cv2.line(img, (0, y), (w - 1, y), 0, 2)
    measured = vertical_tilt(img)
    assert measured is not None and abs(measured - lean) < 0.2
    fixed = unshear(img, measured)
    assert abs(vertical_tilt(fixed)) < 0.15


# ------------------------------------------------------------------ cascade: Vision 2x → Tesseract
from app.ocr.retry import cascade


def reader(zoom: dict, second: dict, calls: list):
    """Fake OCR: per-row readings for each step, keyed by the row's original balance."""
    def read(row, step):
        calls.append(step)
        return (zoom if step == "zoom" else second).get(row.balance)
    return read


def test_cascade_zoom_fixes_misread_without_second_engine():
    rows = [T(debit=200, bal=300), T(credit=50, bal=850)]  # 800 misread as 300
    calls = []
    out, chosen, used = cascade(1000, rows, reader({300: T(debit=200, bal=800),
                                                    850: T(credit=50, bal=850)}, {}, calls))
    assert out == "fixed" and [t.balance for t in chosen] == [800, 850]
    assert not used and "second" not in calls


def test_cascade_zoom_agrees_means_printed_that_way():
    rows = [T(debit=200, bal=300)]  # really printed 300: the statement doesn't add up
    calls = []
    out, chosen, used = cascade(1000, rows, reader({300: T(debit=200, bal=300)}, {}, calls))
    assert out == "verified" and chosen[0].balance == 300 and not used
    assert calls == ["zoom"]


def test_cascade_second_engine_breaks_tie_and_fixes():
    rows = [T(debit=200, bal=300)]
    calls = []
    out, chosen, used = cascade(1000, rows, reader({300: T(debit=200, bal=309)},
                                                   {300: T(debit=200, bal=800)}, calls))
    assert out == "fixed" and chosen[0].balance == 800 and used


def test_cascade_majority_when_nothing_closes():
    rows = [T(debit=200, bal=300)]
    out, chosen, used = cascade(1000, rows, reader({300: T(debit=200, bal=309)},
                                                   {300: T(debit=200, bal=300)}, []))
    assert out == "verified" and chosen[0].balance == 300 and used  # original + tesseract agree


def test_cascade_no_majority_goes_to_review():
    rows = [T(debit=200, bal=300)]
    out, chosen, used = cascade(1000, rows, reader({300: T(debit=200, bal=309)},
                                                   {300: T(debit=200, bal=301)}, []))
    assert out == "unresolved" and chosen[0].balance == 300 and used
