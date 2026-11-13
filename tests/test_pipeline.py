import json
import sqlite3

from solution.pipeline import normalize_phone, parse_date, parse_decimal


def test_phone_normalization_handles_indian_prefixes():
    assert normalize_phone("+91 98765-43210") == "+919876543210"
    assert normalize_phone("09876543210") == "+919876543210"
    assert normalize_phone("9876543210") == "+919876543210"
    assert normalize_phone("12345") is None
    assert normalize_phone("+91 12345 67890") is None


def test_dates_are_day_first_and_impossible_dates_fail():
    assert parse_date("01-Dec-2026") == "2026-12-01"
    assert parse_date("03/10/2026") == "2026-10-03"
    assert parse_date("31/02/2026") is None


def test_decimal_currency_and_rounding():
    assert parse_decimal("Rs. 1,234.565") == "1234.57"
    assert parse_decimal("₹ 0") == "0.00"
    assert parse_decimal("-10") is None

