from datetime import date

import pytest

from app.parsing.values import parse_amount, parse_date, parse_drcr


@pytest.mark.parametrize(
    "text,paise,side",
    [
        ("1,14,454.54", 11445454, None),          # Indian grouping
        ("1,011,783.23", 101178323, None),        # western grouping
        ("Rs. 6,086.63", 608663, None),
        ("Rs 50", 5000, None),
        ("₹ 2,177.93", 217793, None),
        ("INR 17.70", 1770, None),
        ("65,000.00 Cr", 6500000, "credit"),
        ("1,250.00 Dr", 125000, "debit"),
        ("1,250.00DR", 125000, "debit"),
        ("-450.00", 45000, "debit"),
        ("+50,000.00", 5000000, "credit"),
        ("(300.00)", 30000, "debit"),
        ("0.00", 0, None),
        ("17.7", 1770, None),
        # OCR slips
        ("48.551.19 Cr", 4855119, "credit"),
        ("1,52.482.56 Cг", 15248256, "credit"),
        ("1,O11,783.23", 101178323, None),
        ("2,177 .93", 217793, None),
    ],
)
def test_parse_amount(text, paise, side):
    a = parse_amount(text)
    assert a is not None and a.paise == paise and a.side == side


@pytest.mark.parametrize("text", ["", "-", "–", "   ", "NA", "abc", "12/01/2024", "1.2.3", "+", "Rs.", "Rs. -", "(", "()"])
def test_parse_amount_blank_or_invalid(text):
    assert parse_amount(text) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("04-01-2024 12:04:09", date(2024, 1, 4)),
        ("04-01-2024", date(2024, 1, 4)),
        ("02/01/2024", date(2024, 1, 2)),      # day first (India)
        ("02/01/24", date(2024, 1, 2)),
        ("04 Jan 2024", date(2024, 1, 4)),
        ("01-Jan-2025", date(2025, 1, 1)),
        ("19.03.2025", date(2025, 3, 19)),
        ("2024-01-09 09:30:00", date(2024, 1, 9)),
        ("02/01/2024 10:19:55 AM", date(2024, 1, 2)),
        ("5 Sept 2024", date(2024, 9, 5)),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", ["", "B/F", "Page 1", "31/02/2024", "13/13/2024", "S85388609"])
def test_parse_date_invalid(text):
    assert parse_date(text) is None


@pytest.mark.parametrize(
    "text,side",
    [("DR", "debit"), ("Dr", "debit"), ("D", "debit"), ("CR", "credit"), ("cr", "credit"),
     ("C", "credit"), ("", None), ("X", None)],
)
def test_parse_drcr(text, side):
    assert parse_drcr(text) == side
