import pytest

from pocketledger.money import MoneyError, format_amount, format_money, parse_amount, to_decimal_string
from pocketledger.timeutil import local_input_to_utc_iso, range_bounds_utc, utc_iso_to_local
from datetime import date

BDT = {"symbol": "৳", "minor_unit": 2, "grouping": "indian"}


@pytest.mark.parametrize("text,minor", [
    ("1500", 150000), ("1,500", 150000), ("৳ 2,000.50", 200050), ("০১২৫০", 125000), (".5", 50), ("10.", 1000),
    ("Tk 99.99", 9999), ("0", 0), ("12,34,567.89", 123456789),
])
def test_parse_valid(text, minor):
    assert parse_amount(text) == minor


@pytest.mark.parametrize("text", ["", None, "abc", "-5", "1.005", "1e3", "1,2,3x", "NaN", "inf", "5 5 5.5.5"])
def test_parse_rejects(text):
    with pytest.raises(MoneyError):
        parse_amount(text)


def test_parse_rejects_floats_and_bools():
    for bad in (1.5, True):
        with pytest.raises(MoneyError):
            parse_amount(bad)


def test_no_float_drift():
    # 0.1 + 0.2 style traps: integer minor units add exactly
    assert parse_amount("0.10") + parse_amount("0.20") == parse_amount("0.30")
    assert parse_amount("19.99") * 3 == parse_amount("59.97")


def test_format_indian_grouping():
    assert format_amount(123456789, 2, "indian") == "12,34,567.89"
    assert format_amount(500, 2, "indian") == "5.00"
    assert format_amount(100000000, 2, "indian") == "10,00,000.00"
    assert format_amount(123456789, 2, "western") == "1,234,567.89"


def test_format_money_and_negative():
    assert format_money(200000, BDT) == "৳2,000.00"
    assert format_money(-30000, BDT) == "−৳300.00"
    assert to_decimal_string(-30050) == "-300.50"


def test_roundtrip_decimal_string():
    for minor in (0, 1, 99, 100, 123456789):
        assert parse_amount(to_decimal_string(minor)) == minor


def test_dhaka_local_to_utc_and_back():
    assert local_input_to_utc_iso("2026-11-23T14:30", "Asia/Dhaka") == "2026-11-23T08:30:00Z"
    assert utc_iso_to_local("2026-11-23T08:30:00Z", "Asia/Dhaka").strftime("%Y-%m-%d %H:%M") == "2026-11-23 14:30"


def test_day_bounds_are_local_days():
    start, end = range_bounds_utc(date(2026, 11, 23), date(2026, 11, 23), "Asia/Dhaka")
    assert (start, end) == ("2026-11-22T18:00:00Z", "2026-11-23T18:00:00Z")
