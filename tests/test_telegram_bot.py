"""Unit tests for telegram_bot.py pure helpers."""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.telegram_bot import (
    _format_fee_rate_pct,
    _format_net_fragment,
    _format_net_with_ads_line,
    _format_regular_price_line,
    _tier_label,
    col_to_idx,
    cookie_age_days,
    count_statuses,
    extract_dashboard_products,
    extract_last_timestamp,
    format_category_breakdown,
    format_dashboard_reply,
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


# ── new pure helpers (tier label, fee %, sale-savings, net-with-ads) ───────────

def test_tier_label_tier1_at_and_above_seven():
    assert _tier_label("8.2") == "Tier 1 🥇"
    assert _tier_label("7") == "Tier 1 🥇"


def test_tier_label_tier2_between_four_and_seven():
    assert _tier_label("5.0") == "Tier 2"
    assert _tier_label("4") == "Tier 2"


def test_tier_label_tier3_below_four():
    assert _tier_label("2.0") == "Tier 3"
    assert _tier_label("0") == "Tier 3"


def test_tier_label_blank_or_unparseable_returns_none():
    assert _tier_label("") is None
    assert _tier_label(None) is None
    assert _tier_label("n/a") is None


def test_format_net_fragment_custom_label():
    assert _format_net_fragment("$45.20", "18%", label="Net without ads:") == \
        "Net without ads: $45.20 (18%)"


def test_format_net_fragment_default_label_unchanged():
    assert _format_net_fragment("$45.20", "18%") == "net $45.20 (18%)"


def test_format_fee_rate_pct_from_raw_float_string():
    assert _format_fee_rate_pct("0.133") == "13.3%"


def test_format_fee_rate_pct_already_percent_suffixed():
    assert _format_fee_rate_pct("15%") == "15%"


def test_format_fee_rate_pct_blank_returns_none():
    assert _format_fee_rate_pct("") is None
    assert _format_fee_rate_pct(None) is None


def test_format_regular_price_line_shows_savings():
    line = _format_regular_price_line("$449.99", "$389.99")
    assert line == "🏷️ Was $449.99, now $389.99 (save $60.00)"


def test_format_regular_price_line_none_when_blank():
    assert _format_regular_price_line("", "$389.99") is None
    assert _format_regular_price_line(None, "$389.99") is None


def test_format_regular_price_line_none_when_equal_to_current():
    assert _format_regular_price_line("$389.99", "$389.99") is None


def test_format_net_with_ads_line_subtracts_ad_budget():
    line = _format_net_with_ads_line("$45.20", "$6.75", "$459.99")
    assert line == "Net with ads: $38.45 (8%)"


def test_format_net_with_ads_line_treats_blank_ad_budget_as_zero():
    line = _format_net_with_ads_line("$45.20", "", "$459.99")
    assert line == "Net with ads: $45.20 (10%)"


def test_format_net_with_ads_line_guards_zero_ebay_price():
    line = _format_net_with_ads_line("$45.20", "$6.75", "")
    assert line == "Net with ads: $38.45 (—)"


def test_format_net_with_ads_line_none_when_net_profit_unparseable():
    assert _format_net_with_ads_line("", "$6.75", "$459.99") is None


# ── search_products / format_* (/lookup) ───────────────────────────────────

_COL = {
    "status": "A", "title": "C", "category": "D", "stock_status": "F",
    "costco_cost": "G", "ebay_price": "H", "net_profit": "I", "net_margin": "J",
    "last_checked": "O", "costco_url": "R", "sale_info": "X",
    "fee_rate": "AB", "ebay_fees": "AC", "ship_cost": "AD", "ad_cost": "AE",
    "ad_budget": "AH", "regular_price": "AW", "demand_score": "B",
}


def _make_row(**overrides):
    row = [""] * (col_to_idx("AW") + 1)  # A..AW
    defaults = {
        "A": "ACTIVE", "B": "8.2", "C": "PAMP Suisse 1oz Gold Bar", "D": "Precious Metals",
        "F": "In Stock", "G": "$1998.99", "H": "$2199.00", "I": "$180.50",
        "J": "8%", "O": "2026-09-18 08:00", "R": "https://www.costco.com/gold-bar",
        "X": "", "AB": "0.15", "AC": "$329.85", "AD": "$0.00", "AE": "$27.08",
        "AH": "$27.08", "AW": "",
    }
    defaults.update(overrides)
    for col, val in defaults.items():
        row[col_to_idx(col)] = val
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
    assert "Precious Metals · ACTIVE · Score 8.2 (Tier 1 🥇)" in text
    assert "Buy $1998.99" in text
    assert "List $2199.00" in text
    assert "Ship $0.00" in text
    assert "Fees $329.85 (15.0%)" in text
    assert "Ads $27.08 (suggested budget)" in text
    assert "Net without ads: $180.50 (8%)" in text
    assert "Net with ads: $153.42 (7%)" in text
    assert "Stock: In Stock" in text
    assert "Last checked 12h ago" in text
    assert text.endswith("https://www.costco.com/gold-bar")


def test_format_product_detail_no_score_suffix_when_blank():
    p = search_products([_make_row(B="")], _COL, "pamp")[0]
    assert "Score" not in format_product_detail(p)


def test_format_product_detail_savings_line_when_regular_price_differs():
    p = search_products([_make_row(G="$389.99", AW="$449.99")], _COL, "pamp")[0]
    assert "🏷️ Was $449.99, now $389.99 (save $60.00)" in format_product_detail(p)


def test_format_product_detail_no_savings_line_when_regular_price_blank():
    p = search_products([_make_row()], _COL, "pamp")[0]
    assert "🏷️" not in format_product_detail(p)


def test_format_product_detail_ads_line_hidden_when_zero():
    p = search_products([_make_row(AH="$0.00")], _COL, "pamp")[0]
    assert "Ads" not in format_product_detail(p)


def test_format_product_detail_ads_line_hidden_when_blank():
    p = search_products([_make_row(AH="")], _COL, "pamp")[0]
    assert "Ads" not in format_product_detail(p)


def test_format_product_detail_ads_line_hidden_when_negative():
    p = search_products([_make_row(AH="-$5.00")], _COL, "pamp")[0]
    assert "Ads" not in format_product_detail(p)


def test_format_product_detail_expired_sale_shows_expired():
    p = search_products([_make_row(X="🔥 -$150 ends 1/1/26")], _COL, "pamp")[0]
    now = datetime(2026, 9, 18, 12, 0)
    assert "🔥 Sale ended 1/1/26 (expired)" in format_product_detail(p, now=now)


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
    assert "Net without ads: — (—)" in format_product_detail(p)


def test_format_net_fragment_handles_negative_profit():
    p = search_products([_make_row(I="-$12.50", J="-3%")], _COL, "pamp")[0]
    assert "Net without ads: -$12.50 (-3%)" in format_product_detail(p)


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
    "costco_cost": "G", "ebay_price": "H",
    "net_profit": "I", "net_margin": "J", "comp_saturation": "N",
    "suggested_price": "V", "sale_info": "X", "ad_budget": "AH",
    "mpt_sharpe": "AX", "mpt_rank": "BA",
}


def _make_dash_row(**overrides):
    row = [""] * (col_to_idx("BA") + 1)  # A..BA
    defaults = {
        "A": "READY", "B": "8.2", "C": "PAMP Suisse 1oz Gold Bar", "D": "Precious Metals",
        "G": "$1998.99", "H": "$2199.00",
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


def test_format_top_opportunities_shows_buy_list_net_and_ads():
    rows = [_make_dash_row(G="$389.99", H="$459.99", I="$45.20", J="9.8%", AH="$6.75")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_top_opportunities(products)
    assert "Buy $389.99" in text
    assert "List $459.99" in text
    assert "Net $45.20 (9.8%)" in text
    assert "Ads $6.75" in text


def test_format_top_opportunities_tier1_badge_shown():
    rows = [_make_dash_row(B="8.2")]
    products = extract_dashboard_products(rows, _DASH_COL)
    assert "🥇" in format_top_opportunities(products)


def test_format_top_opportunities_no_badge_for_tier2():
    rows = [_make_dash_row(B="5.0")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_top_opportunities(products)
    assert "🥇" not in text


def test_format_top_opportunities_limits_to_n():
    rows = [_make_dash_row(C=f"Item {i}", BA=str(i)) for i in range(1, 6)]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_top_opportunities(products, n=3)
    assert text.count("Item") == 3


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


def test_format_sale_urgency_section_excludes_sale_expired_over_7_days_ago():
    rows = [_make_dash_row(C="Very Stale", X="🔥 -$50 ends 9/01/26")]
    products = extract_dashboard_products(rows, _DASH_COL)
    now = datetime(2026, 9, 18, 12, 0)  # 17 days after 9/01
    assert format_sale_urgency_section(products, now=now) is None


def test_format_sale_urgency_section_includes_sale_expired_within_7_days():
    rows = [_make_dash_row(C="Recently Expired", X="🔥 -$50 ends 9/15/26")]
    products = extract_dashboard_products(rows, _DASH_COL)
    now = datetime(2026, 9, 18, 12, 0)  # 3 days after 9/15
    text = format_sale_urgency_section(products, now=now)
    assert text is not None
    assert "Recently Expired" in text


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


# ── format_dashboard_reply ───────────────────────────────────────────────────

def test_format_dashboard_reply_includes_score_legend():
    counts, total = {"Ready": 1}, 1
    text = format_dashboard_reply(counts, total)
    assert "Scores: 0–10 · Tier 1 ≥7 🥇 · Tier 2 ≥4 · Tier 3 <4 · Sharpe = risk-adjusted return (higher = better)" in text
