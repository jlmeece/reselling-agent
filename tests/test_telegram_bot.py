"""Unit tests for telegram_bot.py pure helpers."""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.telegram_bot import (
    cookie_age_days,
    extract_last_timestamp,
    format_lookup_reply,
    format_product_detail,
    has_errors,
    parse_logs_arg,
    read_tail,
    search_products,
)


# ── read_tail ─────────────────────────────────────────────────────────────────

def test_read_tail_returns_last_n_lines(tmp_path):
    f = tmp_path / "test.log"
    f.write_text("\n".join(f"line {i}" for i in range(50)))
    result = read_tail(str(f), 10)
    assert len(result) == 10
    assert result[-1] == "line 49"
    assert result[0] == "line 40"


def test_read_tail_fewer_lines_than_n(tmp_path):
    f = tmp_path / "test.log"
    f.write_text("line 0\nline 1\nline 2")
    result = read_tail(str(f), 20)
    assert result == ["line 0", "line 1", "line 2"]


def test_read_tail_missing_file_returns_none():
    result = read_tail("/nonexistent/path/test.log", 10)
    assert result is None


def test_read_tail_empty_file(tmp_path):
    f = tmp_path / "empty.log"
    f.write_text("")
    result = read_tail(str(f), 10)
    assert result == []


# ── extract_last_timestamp ────────────────────────────────────────────────────

def test_extract_last_timestamp_finds_loguru_format():
    lines = [
        "2026-05-28 06:00:01.123 | INFO     | __main__:main:52 - Scheduler started",
        "2026-05-28 06:01:15.456 | INFO     | __main__:main:100 - Done",
    ]
    result = extract_last_timestamp(lines)
    assert result == "2026-05-28 06:01:15"


def test_extract_last_timestamp_returns_last_not_first():
    lines = [
        "2026-05-27 01:00:00.000 | INFO - first",
        "2026-05-28 06:00:00.000 | INFO - second",
        "some line with no timestamp",
    ]
    result = extract_last_timestamp(lines)
    assert result == "2026-05-28 06:00:00"


def test_extract_last_timestamp_returns_none_when_no_timestamps():
    lines = ["some line", "another line without a date"]
    result = extract_last_timestamp(lines)
    assert result is None


def test_extract_last_timestamp_empty_list():
    assert extract_last_timestamp([]) is None


# ── has_errors ────────────────────────────────────────────────────────────────

def test_has_errors_detects_error_keyword():
    lines = ["2026-05-28 | INFO - something", "2026-05-28 | ERROR - failed", "INFO - done"]
    assert has_errors(lines) is True


def test_has_errors_detects_traceback():
    lines = [
        "2026-05-28 | INFO - ok",
        "Traceback (most recent call last):",
        '  File "agents/scheduler.py", line 99',
        "ValueError: bad value",
    ]
    assert has_errors(lines) is True


def test_has_errors_clean_log():
    lines = ["2026-05-28 | INFO - all good", "2026-05-28 | INFO - done"]
    assert has_errors(lines) is False


def test_has_errors_empty_list():
    assert has_errors([]) is False


# ── cookie_age_days ───────────────────────────────────────────────────────────

def test_cookie_age_days_returns_float(tmp_path):
    f = tmp_path / "cookies.json"
    f.write_text("{}")
    age = cookie_age_days(str(f))
    assert isinstance(age, float)
    assert age < 1.0  # just created


def test_cookie_age_days_missing_file_returns_none():
    age = cookie_age_days("/nonexistent/cookies.json")
    assert age is None


# ── parse_logs_arg ────────────────────────────────────────────────────────────

def test_parse_logs_arg_valid_modes():
    assert parse_logs_arg("daily") == ("daily", None)
    assert parse_logs_arg("rotation") == ("rotation", None)
    assert parse_logs_arg("active") == ("active", None)


def test_parse_logs_arg_empty_string():
    mode, err = parse_logs_arg("")
    assert mode is None
    assert err is None


def test_parse_logs_arg_none():
    mode, err = parse_logs_arg(None)
    assert mode is None
    assert err is None


def test_parse_logs_arg_whitespace():
    mode, err = parse_logs_arg("  ")
    assert mode is None
    assert err is None


def test_parse_logs_arg_unknown_mode():
    mode, err = parse_logs_arg("badmode")
    assert mode is None
    assert err is not None
    assert "badmode" in err


def test_parse_logs_arg_case_insensitive():
    mode, err = parse_logs_arg("DAILY")
    assert mode == "daily"
    assert err is None


# ── search_products / format_* (/lookup) ───────────────────────────────────

_COL = {
    "status": "A", "title": "C", "category": "D", "stock_status": "F",
    "costco_cost": "G", "ebay_price": "H", "net_profit": "I", "net_margin": "J",
    "last_checked": "O", "costco_url": "R", "sale_info": "X",
}


def _make_row(**overrides):
    row = [""] * 25  # A..Y
    defaults = {
        "A": "ACTIVE", "C": "PAMP Suisse 1oz Gold Bar", "D": "Precious Metals",
        "F": "In Stock", "G": "$1998.99", "H": "$2199.00", "I": "$180.50",
        "J": "8%", "O": "2026-09-18 08:00", "R": "https://www.costco.com/gold-bar",
        "X": "",
    }
    defaults.update(overrides)
    for col, val in defaults.items():
        row[ord(col) - ord("A")] = val
    return row


def test_search_products_matches_title_case_insensitive():
    rows = [_make_row(C="PAMP Suisse Gold Bar")]
    matches = search_products(rows, _COL, "pamp")
    assert len(matches) == 1
    assert matches[0]["title"] == "PAMP Suisse Gold Bar"


def test_search_products_matches_category():
    rows = [_make_row(D="Jewelry")]
    assert len(search_products(rows, _COL, "jewel")) == 1


def test_search_products_no_match():
    rows = [_make_row(C="Gold Bar")]
    assert search_products(rows, _COL, "watch") == []


def test_search_products_skips_blank_rows():
    rows = [[], _make_row(C="Gold Bar")]
    assert len(search_products(rows, _COL, "gold")) == 1


def test_search_products_handles_short_ragged_rows():
    rows = [["ACTIVE", "", "Gold Bar"]]  # only 3 cols
    matches = search_products(rows, _COL, "gold")
    assert len(matches) == 1
    assert matches[0]["category"] == ""
    assert matches[0]["costco_cost"] == ""


def test_format_lookup_reply_zero_matches():
    assert format_lookup_reply([], "xyz") == "No products found matching 'xyz'."


def test_format_lookup_reply_single_match_full_card():
    p = search_products([_make_row()], _COL, "pamp")[0]
    now = datetime(2026, 9, 18, 20, 0)
    text = format_product_detail(p, now=now)
    assert text.startswith("📦 PAMP Suisse 1oz Gold Bar")
    assert "Precious Metals · ACTIVE" in text
    assert "Costco $1998.99 → eBay $2199.00" in text
    assert "net $180.50 (8%)" in text
    assert "Stock: In Stock" in text
    assert "Last checked 12h ago" in text
    assert text.endswith("https://www.costco.com/gold-bar")


def test_format_lookup_reply_multiple_matches_summary():
    rows = [_make_row(C="Gold Bar A"), _make_row(C="Gold Bar B")]
    matches = search_products(rows, _COL, "gold")
    text = format_lookup_reply(matches, "gold")
    assert "Found 2 matches for 'gold':" in text
    assert "• Gold Bar A" in text
    assert "• Gold Bar B" in text
    assert "Be more specific." in text


def test_format_lookup_reply_over_five_matches():
    rows = [_make_row(C=f"Item {i}") for i in range(8)]
    matches = search_products(rows, _COL, "item")
    assert format_lookup_reply(matches, "item") == "Too many matches — be more specific."


def test_format_product_detail_missing_net_shows_dash():
    p = search_products([_make_row(I="", J="")], _COL, "pamp")[0]
    assert "net — (—)" in format_product_detail(p)


def test_format_net_fragment_handles_negative_profit():
    p = search_products([_make_row(I="-$12.50", J="-3%")], _COL, "pamp")[0]
    assert "net -$12.50 (-3%)" in format_product_detail(p)


def test_format_product_detail_stale_after_12h():
    p = search_products([_make_row(O="2026-09-18 06:00")], _COL, "pamp")[0]
    now = datetime(2026, 9, 18, 20, 0)
    assert "STALE" in format_product_detail(p, now=now)


def test_format_product_detail_unknown_last_checked():
    p = search_products([_make_row(O="")], _COL, "pamp")[0]
    assert "Last checked unknown" in format_product_detail(p)


def test_format_product_detail_sale_line_when_parseable():
    p = search_products([_make_row(X="🔥 -$150 ends 12/31/26")], _COL, "pamp")[0]
    now = datetime(2026, 9, 18, 12, 0)
    assert "🔥 Sale ends 12/31/26" in format_product_detail(p, now=now)


def test_format_product_detail_no_sale_line_when_blank():
    p = search_products([_make_row(X="")], _COL, "pamp")[0]
    assert "Sale ends" not in format_product_detail(p)
