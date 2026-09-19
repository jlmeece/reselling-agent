"""Unit tests for telegram_bot.py pure helpers."""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.telegram_bot import (
    col_to_idx,
    cookie_age_days,
    extract_dashboard_products,
    extract_last_timestamp,
    format_ad_budget_section,
    format_category_breakdown,
    format_lookup_reply,
    format_product_detail,
    format_sale_urgency_section,
    format_top_opportunities,
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


# ── /dashboard sections ──────────────────────────────────────────────────────

_DASH_COL = {
    "status": "A", "demand_score": "B", "title": "C", "category": "D",
    "net_profit": "I", "net_margin": "J", "comp_saturation": "N",
    "suggested_price": "V", "sale_info": "X", "ad_budget": "AH",
    "mpt_sharpe": "AX", "mpt_rank": "BA",
}


def _make_dash_row(**overrides):
    row = [""] * (col_to_idx("BA") + 1)  # A..BA
    defaults = {
        "A": "READY", "B": "8.2", "C": "PAMP Suisse 1oz Gold Bar", "D": "Precious Metals",
        "I": "$120.00", "J": "4%", "N": "Low", "V": "$2199.00", "X": "",
        "AH": "$18.00", "AX": "1.4", "BA": "2 🥈 Good",
    }
    defaults.update(overrides)
    for col, val in defaults.items():
        row[col_to_idx(col)] = val
    return row


def test_extract_dashboard_products_skips_blank_status():
    rows = [_make_dash_row(), _make_dash_row(A="")]
    products = extract_dashboard_products(rows, _DASH_COL)
    assert len(products) == 1


def test_extract_dashboard_products_handles_short_ragged_rows():
    rows = [["READY", "", "Gold Bar"]]  # only 3 cols, far short of BA
    products = extract_dashboard_products(rows, _DASH_COL)
    assert len(products) == 1
    assert products[0]["mpt_rank"] == ""


def test_format_top_opportunities_ranks_by_mpt_rank_ascending():
    rows = [
        _make_dash_row(C="Second Place", BA="2"),
        _make_dash_row(C="First Place", BA="1 🥇 Best"),
        _make_dash_row(C="Third Place", BA="3"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_top_opportunities(products)
    assert text.index("First Place") < text.index("Second Place") < text.index("Third Place")
    assert text.startswith("🏆 Top Ready to List")


def test_format_top_opportunities_unranked_sorts_after_ranked():
    rows = [
        _make_dash_row(C="No Rank", BA=""),
        _make_dash_row(C="Ranked", BA="1"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_top_opportunities(products)
    assert text.index("Ranked") < text.index("No Rank")


def test_format_top_opportunities_ignores_non_ready_status():
    rows = [_make_dash_row(A="ACTIVE")]
    products = extract_dashboard_products(rows, _DASH_COL)
    assert format_top_opportunities(products) is None


def test_format_top_opportunities_flags_saturated_comps():
    rows = [_make_dash_row(N="High")]
    products = extract_dashboard_products(rows, _DASH_COL)
    assert "⚠️ saturated" in format_top_opportunities(products)


def test_format_top_opportunities_shows_price_sharpe_and_score():
    rows = [_make_dash_row(V="$2199.00", AX="1.4", B="8.2")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_top_opportunities(products)
    assert "$2199.00" in text
    assert "Sharpe 1.4" in text
    assert "Score 8.2" in text


def test_format_top_opportunities_limits_to_n():
    rows = [_make_dash_row(C=f"Item {i}", BA=str(i)) for i in range(1, 6)]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_top_opportunities(products, n=3)
    assert text.count("Item") == 3


def test_format_ad_budget_section_sums_ready_rows():
    rows = [
        _make_dash_row(I="$100.00", AH="$15.00"),
        _make_dash_row(I="$50.00", AH="$8.00"),
        _make_dash_row(A="ACTIVE", I="$999.00", AH="$999.00"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_ad_budget_section(products)
    assert "Total net if all Ready listed: $150" in text
    assert "Suggested ad budget (15%): $23" in text


def test_format_ad_budget_section_none_when_no_ready():
    rows = [_make_dash_row(A="ACTIVE")]
    products = extract_dashboard_products(rows, _DASH_COL)
    assert format_ad_budget_section(products) is None


def test_format_sale_urgency_section_sorts_soonest_first():
    rows = [
        _make_dash_row(C="Later Sale", X="🔥 -$50 ends 12/31/26"),
        _make_dash_row(C="Sooner Sale", X="🔥 -$50 ends 10/01/26"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    now = datetime(2026, 9, 18, 12, 0)
    text = format_sale_urgency_section(products, now=now)
    assert text.index("Sooner Sale") < text.index("Later Sale")


def test_format_sale_urgency_section_none_when_no_sales():
    rows = [_make_dash_row(X="")]
    products = extract_dashboard_products(rows, _DASH_COL)
    assert format_sale_urgency_section(products) is None


def test_format_sale_urgency_section_ignores_pending_status():
    rows = [_make_dash_row(A="PENDING", X="🔥 -$50 ends 12/31/26")]
    products = extract_dashboard_products(rows, _DASH_COL)
    assert format_sale_urgency_section(products) is None


def test_format_category_breakdown_counts_ready_and_active():
    rows = [
        _make_dash_row(D="Precious Metals", A="READY"),
        _make_dash_row(D="Precious Metals", A="READY"),
        _make_dash_row(D="Precious Metals", A="ACTIVE"),
        _make_dash_row(D="Jewelry", A="READY"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_category_breakdown(products, category_names=["Precious Metals", "Jewelry"])
    assert "Precious Metals: 2 Ready, 1 Active" in text
    assert "Jewelry: 1 Ready" in text


def test_format_category_breakdown_skips_categories_with_no_ready_or_active():
    rows = [_make_dash_row(D="Jewelry", A="PENDING")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_category_breakdown(products, category_names=["Precious Metals", "Jewelry"])
    assert text is None


def test_format_category_breakdown_appends_unknown_category():
    rows = [_make_dash_row(D="New Category", A="READY")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_category_breakdown(products, category_names=["Precious Metals"])
    assert "New Category: 1 Ready" in text
