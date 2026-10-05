"""Unit tests for telegram_bot.py pure helpers."""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.telegram_bot import (
    PROTECTED_COLS,
    _active_net_key,
    _extract_rows_by_field,
    _format_active_line,
    _parse_pct,
    compute_category_roi,
    compute_spot_price_impact,
    extract_audit_queue,
    extract_review_queue,
    find_back_in_stock,
    find_stale_active_items,
    partition_stock,
    format_audit_card,
    format_review_card,
    _format_fee_rate_pct,
    _format_net_fragment,
    _format_net_with_ads_line,
    _format_regular_price_line,
    _is_duplicate_instance,
    _tier_label,
    col_to_idx,
    cookie_age_days,
    count_statuses,
    extract_dashboard_products,
    extract_last_timestamp,
    format_category_breakdown,
    format_dashboard_reply,
    format_lookup_reply,
    format_on_sale_count,
    format_product_detail,
    format_queue_dump,
    format_sale_urgency_section,
    format_sales_screen,
    format_top_opportunities,
    has_errors,
    parse_logs_arg,
    read_tail,
    safe_get,
    safe_write_row,
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


# ── PID lockfile duplicate-instance predicate ───────────────────────────────

def test_is_duplicate_instance_true_when_other_pid_alive():
    assert _is_duplicate_instance(old_pid=999, current_pid=111, pid_exists_fn=lambda p: True) is True


def test_is_duplicate_instance_false_when_other_pid_dead():
    assert _is_duplicate_instance(old_pid=999, current_pid=111, pid_exists_fn=lambda p: False) is False


def test_is_duplicate_instance_false_when_pid_is_self():
    # os.execv() (used by /restart) keeps the same PID — the file will contain
    # our own PID, which must never be treated as "another instance".
    assert _is_duplicate_instance(old_pid=111, current_pid=111, pid_exists_fn=lambda p: True) is False


def test_is_duplicate_instance_false_when_no_pid_file():
    assert _is_duplicate_instance(old_pid=None, current_pid=111, pid_exists_fn=lambda p: True) is False


# ── new pure helpers (tier label, fee %, sale-savings, net-with-ads) ───────────

def test_tier_label_tier1_at_and_above_six():
    assert _tier_label("8.2") == "Tier 1 🥇"
    assert _tier_label("6") == "Tier 1 🥇"


def test_tier_label_tier2_between_three_and_six():
    assert _tier_label("5.9") == "Tier 2"
    assert _tier_label("3") == "Tier 2"


def test_tier_label_tier3_below_three():
    assert _tier_label("2.9") == "Tier 3"
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


def test_format_net_fragment_normalizes_raw_decimal_margin_and_net():
    assert _format_net_fragment("94.33615", "0.5517056553") == "net $94.34 (55%)"


def test_format_net_fragment_margin_already_percent_untouched():
    assert _format_net_fragment("$73.75", "12%") == "net $73.75 (12%)"


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
    assert line == "Net after reserve: $38.45 (8%)"


def test_format_net_with_ads_line_treats_blank_ad_budget_as_zero():
    line = _format_net_with_ads_line("$45.20", "", "$459.99")
    assert line == "Net after reserve: $45.20 (10%)"


def test_format_net_with_ads_line_guards_zero_ebay_price():
    line = _format_net_with_ads_line("$45.20", "$6.75", "")
    assert line == "Net after reserve: $38.45 (—)"


def test_format_net_with_ads_line_none_when_net_profit_unparseable():
    assert _format_net_with_ads_line("", "$6.75", "$459.99") is None


# ── _extract_rows_by_field (shared row-extraction helper) ───────────────────

_COL = {
    "status": "A", "title": "C", "category": "D", "stock_status": "F",
    "costco_cost": "G", "ebay_price": "H", "net_profit": "I", "net_margin": "J",
    "sold_90d": "K",
    "last_checked": "O", "costco_url": "R", "sale_info": "X",
    "fee_rate": "AB", "ebay_fees": "AC", "ship_cost": "AD", "ad_cost": "AE",
    "ad_budget": "AH", "regular_price": "AW", "demand_score": "B",
    "tier_summary": "T",
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


def test_extract_rows_by_field_includes_row_num():
    rows = [_make_row(C="Gold Bar")]
    items = _extract_rows_by_field(rows, _COL, ("title",), data_start_row=4)
    assert items[0]["row_num"] == 4


def test_extract_rows_by_field_row_num_offset_by_position():
    rows = [_make_row(C="First"), _make_row(C="Second")]
    items = _extract_rows_by_field(rows, _COL, ("title",), data_start_row=4)
    assert items[0]["row_num"] == 4
    assert items[1]["row_num"] == 5


def test_extract_rows_by_field_skips_blank_rows_but_row_num_still_counts_position():
    rows = [[], _make_row(C="Second")]
    items = _extract_rows_by_field(rows, _COL, ("title",), data_start_row=4)
    assert len(items) == 1
    assert items[0]["row_num"] == 5


def test_extract_rows_by_field_applies_filter_fn():
    rows = [_make_row(A="PENDING"), _make_row(A="SCORED")]
    status_i = col_to_idx(_COL["status"])
    items = _extract_rows_by_field(
        rows, _COL, ("title",), data_start_row=4,
        filter_fn=lambda row: safe_get(row, status_i) == "SCORED",
    )
    assert len(items) == 1
    assert items[0]["title"] == "PAMP Suisse 1oz Gold Bar"


def test_extract_rows_by_field_never_raises_on_ragged_rows():
    rows = [["ACTIVE"]]  # far short of every requested field's column
    items = _extract_rows_by_field(rows, _COL, ("title", "category"), data_start_row=4)
    assert items[0]["title"] == ""
    assert items[0]["category"] == ""


def test_extract_rows_by_field_no_filter_returns_all_nonblank_rows():
    rows = [_make_row(C="A"), _make_row(C="B")]
    items = _extract_rows_by_field(rows, _COL, ("title",), data_start_row=4)
    assert len(items) == 2


# ── extract_review_queue / extract_audit_queue ───────────────────────────────

def test_extract_review_queue_filters_scored_status():
    rows = [_make_row(A="SCORED", C="Scored Item"), _make_row(A="PENDING", C="Pending Item")]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    assert len(items) == 1
    assert items[0]["title"] == "Scored Item"


def test_extract_review_queue_sorts_by_monthly_profit_descending():
    rows = [
        _make_row(A="SCORED", C="Low Monthly", I="$4.00", K="3"),     # 4 × 1 = $4/mo
        _make_row(A="SCORED", C="High Monthly", I="$10.00", K="30"),  # 10 × 10 = $100/mo
    ]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    assert [i["title"] for i in items] == ["High Monthly", "Low Monthly"]


def test_extract_review_queue_tiebreaks_by_net_per_unit():
    rows = [
        _make_row(A="SCORED", C="Fast Cheap", I="$10.00", K="15"),  # 10 × 5 = $50/mo
        _make_row(A="SCORED", C="Slow Rich", I="$50.00", K="3"),    # 50 × 1 = $50/mo
    ]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    # Equal monthly profit ($50/mo); higher net/unit ($50) sorts first
    assert [i["title"] for i in items] == ["Slow Rich", "Fast Cheap"]


def test_extract_review_queue_zero_velocity_sorts_last():
    rows = [
        _make_row(A="SCORED", C="No Velocity", I="$10.00", K=""),
        _make_row(A="SCORED", C="Has Velocity", I="$10.00", K="30"),
    ]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    assert [i["title"] for i in items] == ["Has Velocity", "No Velocity"]


def test_extract_review_queue_includes_row_num():
    rows = [_make_row(A="PENDING"), _make_row(A="SCORED", C="Item")]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    assert items[0]["row_num"] == 5


def test_extract_review_queue_excludes_net_profit_below_floor():
    rows = [
        _make_row(A="SCORED", C="Too Low", I="$2.00"),
        _make_row(A="SCORED", C="Meets Floor", I="$4.00"),
    ]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    assert [i["title"] for i in items] == ["Meets Floor"]


def test_extract_review_queue_excludes_blank_net_profit():
    rows = [_make_row(A="SCORED", C="No Net Profit", I="")]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    assert items == []


def test_extract_audit_queue_filters_audit_review_status():
    rows = [_make_row(A="AUDIT_REVIEW", C="Flagged"), _make_row(A="SCORED", C="Not Flagged")]
    items = extract_audit_queue(rows, _COL, data_start_row=4)
    assert len(items) == 1
    assert items[0]["title"] == "Flagged"


def test_extract_audit_queue_includes_tier_summary():
    rows = [_make_row(A="AUDIT_REVIEW", T="[AUDIT_REVIEW] Stale 90+ days")]
    items = extract_audit_queue(rows, _COL, data_start_row=4)
    assert items[0]["tier_summary"] == "[AUDIT_REVIEW] Stale 90+ days"


def test_extract_audit_queue_preserves_sheet_row_order():
    rows = [
        _make_row(A="AUDIT_REVIEW", C="First"),
        _make_row(A="AUDIT_REVIEW", C="Second"),
    ]
    items = extract_audit_queue(rows, _COL, data_start_row=4)
    assert [i["title"] for i in items] == ["First", "Second"]
    assert items[0]["row_num"] == 4
    assert items[1]["row_num"] == 5


# ── format_review_card / format_audit_card ───────────────────────────────────

def test_format_review_card_shows_position_and_body():
    p = search_products([_make_row(A="SCORED")], _COL, "pamp")[0]
    text = format_review_card(p, 1, 5)
    assert text.startswith("Item 1 of 5")
    assert "📦 PAMP Suisse 1oz Gold Bar" in text


def test_format_audit_card_shows_flag_reason():
    rows = [_make_row(A="AUDIT_REVIEW", T="[AUDIT_REVIEW] Stale 90+ days")]
    p = extract_audit_queue(rows, _COL, data_start_row=4)[0]
    text = format_audit_card(p, 1, 3)
    assert "⚠️ Flagged: [AUDIT_REVIEW] Stale 90+ days" in text
    assert "Item 1 of 3" in text


def test_format_audit_card_no_flag_line_when_tier_summary_blank():
    rows = [_make_row(A="AUDIT_REVIEW", T="")]
    p = extract_audit_queue(rows, _COL, data_start_row=4)[0]
    text = format_audit_card(p, 1, 1)
    assert "Flagged" not in text


# ── format_queue_dump ────────────────────────────────────────────────────────

def test_format_queue_dump_shows_scraped_sale_end_date():
    rows = [_make_row(A="SCORED", C="Energy Shot", D="Pharmacy", I="$18.20", X="🔥 -$8 ends 10/18/26")]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    chunks = format_queue_dump(items, kind="review", now=datetime(2026, 9, 30, 12, 0))
    assert len(chunks) == 1
    assert "Energy Shot" in chunks[0]
    assert "net $18.20" in chunks[0]
    assert "ends 10/18/26" in chunks[0]


def test_format_queue_dump_never_guesses_missing_date():
    rows = [_make_row(A="AUDIT_REVIEW", C="No Sale Item", D="Jewelry", I="$3.50", X="", T="zero velocity")]
    items = extract_audit_queue(rows, _COL, data_start_row=4)
    chunks = format_queue_dump(items, kind="audit", now=datetime(2026, 9, 30, 12, 0))
    assert "No Sale Item" in chunks[0]
    assert "no sale" in chunks[0]
    assert "zero velocity" in chunks[0]


def test_format_queue_dump_chunks_long_queues():
    rows = [_make_row(A="SCORED", C=f"Item number {n}", I="$10.00", X="🔥 -$8 ends 10/18/26") for n in range(80)]
    items = extract_review_queue(rows, _COL, data_start_row=4)
    chunks = format_queue_dump(items, kind="review", now=datetime(2026, 9, 30, 12, 0))
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)


# ── safe_write_row / PROTECTED_COLS ──────────────────────────────────────────

def test_protected_cols_matches_col_map_formula_columns():
    assert PROTECTED_COLS == {"I", "J", "N", "Z", "AC", "AF", "AG", "AH"}


def test_safe_write_row_raises_on_protected_column(monkeypatch):
    calls = []
    monkeypatch.setattr("tools.sheet_writer.write_row_partial", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ValueError):
        safe_write_row(None, "Product Tracker", 42, [("J", "0.5")])
    assert calls == []


def test_safe_write_row_raises_when_any_pair_is_protected():
    with pytest.raises(ValueError):
        safe_write_row(None, "Product Tracker", 42, [("A", "APPROVED"), ("AC", "1.00")])


def test_safe_write_row_case_insensitive_protected_check(monkeypatch):
    monkeypatch.setattr("tools.sheet_writer.write_row_partial", lambda *a, **k: None)
    with pytest.raises(ValueError):
        safe_write_row(None, "Product Tracker", 42, [("j", "0.5")])


def test_safe_write_row_passes_through_clean_columns(monkeypatch):
    calls = []
    monkeypatch.setattr("tools.sheet_writer.write_row_partial", lambda *a, **k: calls.append((a, k)))
    safe_write_row(None, "Product Tracker", 42, [("A", "APPROVED")])
    assert len(calls) == 1


# ── search_products / format_* (/lookup) ───────────────────────────────────

def test_search_products_row_num_reflects_absolute_sheet_row():
    rows = [_make_row(C="First"), _make_row(C="PAMP match")]
    matches = search_products(rows, _COL, "pamp", data_start_row=4)
    assert len(matches) == 1
    assert matches[0]["row_num"] == 5


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
    assert "Profit reserve $27.08 (15% of net profit)" in text
    assert "Net before reserve: $180.50 (8%)" in text
    assert "Net after reserve: $153.42 (7%)" in text
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
    assert "Net before reserve: — (—)" in format_product_detail(p)


def test_format_net_fragment_handles_negative_profit():
    p = search_products([_make_row(I="-$12.50", J="-3%")], _COL, "pamp")[0]
    assert "Net before reserve: -$12.50 (-3%)" in format_product_detail(p)


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
    "stock_status": "F", "costco_cost": "G", "ebay_price": "H",
    "net_profit": "I", "net_margin": "J", "comp_saturation": "N",
    "suggested_price": "V", "sale_info": "X", "ad_budget": "AH",
    "last_checked": "O", "mpt_sharpe": "AX", "mpt_rank": "BA",
    "regular_price": "AW", "ebay_listing_url": "Q", "sku": "AA",
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


def test_extract_dashboard_products_row_num_reflects_absolute_sheet_row():
    rows = [_make_dash_row(A=""), _make_dash_row(C="Second")]
    products = extract_dashboard_products(rows, _DASH_COL, data_start_row=4)
    assert len(products) == 1
    assert products[0]["row_num"] == 5


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


def test_format_top_opportunities_shows_buy_list_net_and_reserve():
    rows = [_make_dash_row(G="$389.99", H="$459.99", I="$45.20", J="9.8%", AH="$6.75")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_top_opportunities(products)
    assert "Buy $389.99" in text
    assert "List $459.99" in text
    assert "Net $45.20 (9.8%)" in text
    assert "Reserve $6.75" in text


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
        _make_dash_row(C="Later Sale", X="🔥 -$50 ends 9/24/26"),   # both inside the 7-day window
        _make_dash_row(C="Sooner Sale", X="🔥 -$50 ends 9/20/26"),
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


def test_format_sales_screen_groups_urgent_first():
    rows = [
        _make_dash_row(C="Later", X="🔥 -$50 ends 12/31/26"),
        _make_dash_row(C="This Week", X="🔥 -$8 ends 10/4/26"),
        _make_dash_row(C="Ends Today", X="🔥 -$5 ends 9/30/26"),
        _make_dash_row(C="Just Ended", X="🔥 -$5 ends 9/29/26"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    now = datetime(2026, 9, 30, 12, 0)
    text = format_sales_screen(products, now=now)
    assert text is not None
    assert "On sale: 4" in text
    assert "0 live" in text
    assert "⬜ NOT LISTED (4)" in text
    assert "ends TODAY" in text
    assert "ended 9/29" in text
    # ends-today sorts before the already-ended item, both before this-week and later
    assert text.index("Ends Today") < text.index("Just Ended")
    assert text.index("This Week") < text.index("Later")


def test_format_sales_screen_splits_live_vs_not_listed():
    rows = [
        _make_dash_row(A="ACTIVE", C="Live Item", X="🔥 -$5 ends 10/4/26"),
        _make_dash_row(A="READY", C="Pipeline Item", X="🔥 -$5 ends 10/4/26"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_sales_screen(products, now=datetime(2026, 9, 30, 12, 0))
    assert "🟩 LIVE ON EBAY (1)" in text
    assert "⬜ NOT LISTED (1)" in text
    assert "Live Item" in text
    assert "Pipeline Item" in text
    assert text.index("🟩") < text.index("⬜")


def test_format_sales_screen_reprice_guidance_for_ended_live_sale():
    rows = [
        _make_dash_row(A="ACTIVE", C="Gold Bar", G="$2000.00", H="$2200.00",
                       AW="$2100.00", X="🔥 -$100 ends 9/29/26"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_sales_screen(products, now=datetime(2026, 9, 30, 12, 0))
    assert text is not None
    assert "🟩 LIVE ON EBAY (1)" in text
    assert "ended 9/29" in text
    # bump = 2100 - 2000 = 100, so eBay should move 2200 -> 2300
    assert "cost → $2,100, eBay → $2,300" in text


def test_format_sales_screen_no_reprice_for_not_listed_ended():
    rows = [
        _make_dash_row(A="READY", C="Missed Deal", G="$50.00", AW="$80.00",
                       X="🔥 -$30 ends 9/29/26"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_sales_screen(products, now=datetime(2026, 9, 30, 12, 0))
    # not listed -> no reprice guidance, just "deal over" in the legend
    assert "⬜ NOT LISTED (1)" in text
    assert "cost → $80" not in text


def test_format_sales_screen_shows_sku_when_present():
    rows = [_make_dash_row(C="Gold Bar", AA="1700000", X="🔥 -$5 ends 10/4/26")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_sales_screen(products, now=datetime(2026, 9, 30, 12, 0))
    assert "#1700000" in text


def test_sku_tag():
    from agents.telegram_bot import _sku_tag
    assert _sku_tag("1700000") == " #1700000"
    assert _sku_tag("") == ""
    assert _sku_tag(None) == ""


def test_format_sales_screen_includes_sheet_row_number():
    rows = [_make_dash_row(C="Energy Shot", X="🔥 -$8 ends 10/18/26")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_sales_screen(products, now=datetime(2026, 9, 30, 12, 0))
    assert "r4 ·" in text  # first data row = absolute sheet row 4


def test_format_sales_screen_condenses_long_titles():
    long_title = "Bosch 800 Series Fully Automatic Espresso, Coffee and Cold Brew Machine"
    rows = [_make_dash_row(C=long_title, X="🔥 -$8 ends 10/18/26")]
    products = extract_dashboard_products(rows, _DASH_COL)
    text = format_sales_screen(products, now=datetime(2026, 9, 30, 12, 0))
    assert "…" in text
    assert long_title not in text


def test_format_sales_screen_none_when_no_sales():
    rows = [_make_dash_row(X="")]
    products = extract_dashboard_products(rows, _DASH_COL)
    assert format_sales_screen(products) is None


def test_format_sales_screen_drops_stale_expired():
    rows = [_make_dash_row(C="Dead Sale", X="🔥 -$5 ends 9/01/26")]
    products = extract_dashboard_products(rows, _DASH_COL)
    now = datetime(2026, 9, 30, 12, 0)  # 29 days after 9/01
    assert format_sales_screen(products, now=now) is None


def test_format_on_sale_count_counts_live_sales_only():
    rows = [
        _make_dash_row(C="On Sale", X="🔥 -$8 ends 10/18/26"),
        _make_dash_row(C="No Sale", X=""),
        _make_dash_row(C="Expired", X="🔥 -$5 ends 9/01/26"),
    ]
    products = extract_dashboard_products(rows, _DASH_COL)
    now = datetime(2026, 9, 30, 12, 0)
    assert format_on_sale_count(products, now=now) == "🔥 On sale: 1"
    assert format_on_sale_count(products[:1], now=now) == "🔥 On sale: 1"
    assert format_on_sale_count(products[1:2], now=now) is None


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
    assert "Scores: 0–10 · Tier 1 ≥6 🥇 · Tier 2 ≥3 · Tier 3 <3 · Sharpe = risk-adjusted return (higher = better)" in text


# ── _parse_pct ────────────────────────────────────────────────────────────────

def test_parse_pct_from_percent_suffixed():
    assert _parse_pct("18%") == 18.0


def test_parse_pct_from_raw_fraction():
    assert _parse_pct("0.18") == 18.0


def test_parse_pct_from_plain_number():
    assert _parse_pct("18") == 18.0


def test_parse_pct_blank_returns_none():
    assert _parse_pct("") is None
    assert _parse_pct(None) is None


# ── compute_category_roi ─────────────────────────────────────────────────────

def test_compute_category_roi_header():
    text = compute_category_roi([], category_names=[])
    assert text.startswith("📈 Category ROI")


def test_compute_category_roi_includes_zero_item_category():
    text = compute_category_roi([], category_names=["Precious Metals", "Jewelry"])
    assert "Precious Metals: 0 items" in text
    assert "Jewelry: 0 items" in text


def test_compute_category_roi_averages_net_margin():
    products = [
        {"status": "READY", "category": "Jewelry", "net_margin": "10%"},
        {"status": "ACTIVE", "category": "Jewelry", "net_margin": "20%"},
    ]
    text = compute_category_roi(products, category_names=["Jewelry"])
    assert "Jewelry: 15% avg margin (2 items)" in text


def test_compute_category_roi_excludes_paused_and_rejected():
    products = [
        {"status": "PAUSED_OOS", "category": "Jewelry", "net_margin": "10%"},
        {"status": "REJECTED", "category": "Jewelry", "net_margin": "50%"},
        {"status": "READY", "category": "Jewelry", "net_margin": "20%"},
    ]
    text = compute_category_roi(products, category_names=["Jewelry"])
    assert "Jewelry: 20% avg margin (1 items)" in text


def test_compute_category_roi_appends_unknown_category():
    products = [{"status": "READY", "category": "New Category", "net_margin": "10%"}]
    text = compute_category_roi(products, category_names=["Jewelry"])
    assert "New Category: 10% avg margin (1 items)" in text


# ── compute_spot_price_impact ─────────────────────────────────────────────────

def test_compute_spot_price_impact_header_shows_spot_prices():
    text = compute_spot_price_impact([], gold_spot=2650.0, silver_spot=31.2)
    assert "Gold: $2,650.00/oz" in text
    assert "Silver: $31.20/oz" in text
    assert text.startswith("🪙 Spot Price Impact")


def test_compute_spot_price_impact_estimates_parseable_gold_item():
    products = [{
        "status": "ACTIVE", "category": "Precious Metals",
        "title": "1 oz Gold Bar PAMP Suisse", "costco_cost": "$1998.99",
        "ebay_price": "$2199.00", "net_margin": "8%",
    }]
    text = compute_spot_price_impact(products, gold_spot=2000.0, silver_spot=25.0)
    assert "1 Precious Metals items re-estimated · 0 skipped (no parseable weight)" in text
    assert "PAMP Suisse" in text
    assert "was 8%" in text


def test_compute_spot_price_impact_skips_unparseable_weight():
    products = [{
        "status": "ACTIVE", "category": "Precious Metals",
        "title": "Diamond Tennis Bracelet", "costco_cost": "$500", "ebay_price": "$700",
    }]
    text = compute_spot_price_impact(products, gold_spot=2000.0, silver_spot=25.0)
    assert "0 Precious Metals items re-estimated · 1 skipped (no parseable weight)" in text


def test_compute_spot_price_impact_ignores_non_active_status():
    products = [{
        "status": "PENDING", "category": "Precious Metals",
        "title": "1 oz Gold Bar", "costco_cost": "$1998.99", "ebay_price": "$2199.00",
    }]
    text = compute_spot_price_impact(products, gold_spot=2000.0, silver_spot=25.0)
    assert "0 Precious Metals items re-estimated · 0 skipped (no parseable weight)" in text


def test_compute_spot_price_impact_ignores_non_precious_metals_category():
    products = [{
        "status": "ACTIVE", "category": "Jewelry",
        "title": "1 oz Gold Bar", "costco_cost": "$1998.99", "ebay_price": "$2199.00",
    }]
    text = compute_spot_price_impact(products, gold_spot=2000.0, silver_spot=25.0)
    assert "0 Precious Metals items re-estimated · 0 skipped (no parseable weight)" in text


def test_compute_spot_price_impact_uses_silver_spot_for_silver_titles():
    products = [{
        "status": "ACTIVE", "category": "Precious Metals",
        "title": "10 oz Silver Bar", "costco_cost": "$250.00", "ebay_price": "$320.00",
    }]
    text = compute_spot_price_impact(products, gold_spot=2000.0, silver_spot=30.0)
    assert "margin ~6%" in text


# ── find_stale_active_items / find_back_in_stock (Alerts screen) ────────────

def test_find_stale_active_items_flags_over_12h():
    products = [{"status": "ACTIVE", "last_checked": "2026-09-18 06:00", "title": "Old"}]
    now = datetime(2026, 9, 18, 20, 0)
    assert len(find_stale_active_items(products, now=now)) == 1


def test_find_stale_active_items_ignores_fresh_check():
    products = [{"status": "ACTIVE", "last_checked": "2026-09-18 19:00", "title": "Fresh"}]
    now = datetime(2026, 9, 18, 20, 0)
    assert find_stale_active_items(products, now=now) == []


def test_find_stale_active_items_ignores_non_active():
    products = [{"status": "PENDING", "last_checked": "2026-09-18 06:00"}]
    now = datetime(2026, 9, 18, 20, 0)
    assert find_stale_active_items(products, now=now) == []


def test_find_back_in_stock_flags_recovered_stock():
    products = [{"status": "PAUSED_OOS", "stock_status": "In Stock", "title": "Back"}]
    assert len(find_back_in_stock(products)) == 1


def test_find_back_in_stock_ignores_still_out_of_stock():
    products = [{"status": "PAUSED_OOS", "stock_status": "OUT OF STOCK"}]
    assert find_back_in_stock(products) == []


def test_find_back_in_stock_ignores_non_paused_oos_status():
    products = [{"status": "ACTIVE", "stock_status": "In Stock"}]
    assert find_back_in_stock(products) == []


def test_partition_stock_splits_oos_and_back_in_stock():
    products = [
        {"status": "ACTIVE", "stock_status": "OUT OF STOCK", "title": "Live OOS"},
        {"status": "PAUSED_OOS", "stock_status": "OUT OF STOCK", "title": "Still OOS"},
        {"status": "PAUSED_OOS", "stock_status": "In Stock", "title": "Recovered"},
        {"status": "READY", "stock_status": "OUT OF STOCK", "title": "Pipeline OOS"},
    ]
    oos, back = partition_stock(products)
    assert {p["title"] for p in back} == {"Recovered"}
    assert {p["title"] for p in oos} == {"Live OOS", "Still OOS", "Pipeline OOS"}


def test_partition_stock_orders_live_listings_first():
    products = [
        {"status": "PAUSED_OOS", "stock_status": "OUT OF STOCK", "title": "Paused"},
        {"status": "ACTIVE", "stock_status": "OUT OF STOCK", "title": "Live"},
    ]
    oos, _ = partition_stock(products)
    assert oos[0]["title"] == "Live"


# ── Active Listings helpers ───────────────────────────────────────────────────

def test_format_active_line_renders_price_net_units():
    p = {
        "title": "Kirkland Energy Shot",
        "category": "Pharmacy",
        "costco_cost": "$39.99",
        "ebay_price": "$53.99",
        "net_profit": "$6.85",
        "net_margin": "13%",
        "units_sold": "2",
    }
    assert _format_active_line(p) == "💊 Kirkland Energy Shot\n   🛒 $39.99 → 🏷️ $53.99 · 💰 net $6.85 (13%) · 📦 sold 2"


def test_format_active_line_blank_units_default_to_zero():
    p = {"title": "CoQ10", "ebay_price": "$43.99", "net_profit": "$11.17",
         "net_margin": "25%", "units_sold": ""}
    assert _format_active_line(p).endswith("📦 sold 0")


def test_format_active_line_unknown_category_falls_back_to_box():
    p = {"title": "Mystery", "category": "Toys", "ebay_price": "$10.00",
         "net_profit": "$2.00", "net_margin": "20%", "units_sold": "1"}
    assert _format_active_line(p).startswith("📦 Mystery")


def test_active_net_key_sorts_desc_with_unknown_last():
    items = [
        {"net_profit": ""},
        {"net_profit": "$6.85"},
        {"net_profit": "$11.17"},
    ]
    items.sort(key=_active_net_key)
    assert [i["net_profit"] for i in items] == ["$11.17", "$6.85", ""]


# ── Sale Expiring Soon ────────────────────────────────────────────────────────

def test_sale_urgency_excludes_sales_ending_more_than_a_week_out():
    now = datetime(2026, 9, 29, 12, 0)
    products = [{"status": "ACTIVE", "sale_info": "🔥 -$5 ends 10/20/26", "title": "Far Out"}]
    assert format_sale_urgency_section(products, now=now) is None


def test_sale_urgency_includes_sales_ending_soon():
    now = datetime(2026, 9, 29, 12, 0)
    products = [{"status": "ACTIVE", "sale_info": "🔥 -$5 ends 10/01/26", "title": "Ends Soon"}]
    out = format_sale_urgency_section(products, now=now)
    assert out is not None and "Ends Soon" in out
