"""
Tests for the audit Telegram message builder (agents/auditor.py).

_build_audit_message is a pure function (no I/O) — no mocking needed.
"""
import sys
sys.path.insert(0, ".")

from agents.auditor import _build_audit_message, _truncate, _fmt_line


def _product(title, **overrides):
    p = {"title": title, "category": "Precious Metals"}
    p.update(overrides)
    return p


# ── _truncate ────────────────────────────────────────────────────────────────

def test_truncate_leaves_short_strings_untouched():
    assert _truncate("short title", 45) == "short title"


def test_truncate_cuts_long_strings_with_ellipsis():
    long_title = "A" * 60
    result = _truncate(long_title, 45)
    assert len(result) == 45
    assert result.endswith("…")


# ── _fmt_line / HTML escaping ──────────────────────────────────────────────

def test_fmt_line_escapes_html_special_chars():
    line = _fmt_line(_product("Bacon & Cheese <Kit>"), "Negative net (<$1.00)")
    assert "&amp;" in line
    assert "&lt;Kit&gt;" in line
    assert "<Kit>" not in line
    assert "<$1.00)" not in line


def test_fmt_line_truncates_before_escaping_so_entities_are_not_split():
    # "&" sits right at the truncation boundary — truncating the escaped
    # "&amp;" instead of the raw "&" would produce a broken entity like "&am".
    title = "A" * 44 + "&" + "B" * 20
    line = _fmt_line(_product(title), "reason")
    assert "&am" not in line.replace("&amp;", "")  # no half-written entity


# ── _build_audit_message ─────────────────────────────────────────────────────

def test_build_message_lists_removed_and_flagged_products_with_reasons():
    to_remove = [(10, _product("Gold Bar 1oz"), "Negative net profit ($-2.50)")]
    to_flag   = [(20, _product("Silver Coin 5pk"), "Borderline net ($0.75) — below $1 floor")]

    text = _build_audit_message("2026-09-18", to_remove, to_flag, n_subs=1,
                                 category_health={"Precious Metals": 80})

    assert "Auto-removed: 1 rows" in text
    assert "• Gold Bar 1oz — Negative net profit ($-2.50)" in text
    assert "Flagged for review: 1 rows" in text
    assert "• Silver Coin 5pk — Borderline net ($0.75) — below $1 floor" in text
    assert "Categories needing discovery: 1" in text


def test_build_message_caps_removed_at_8_and_flagged_at_5_with_more_line():
    to_remove = [(i, _product(f"Product {i}"), "reason") for i in range(12)]
    to_flag   = [(i, _product(f"Flag {i}"), "reason") for i in range(7)]

    text = _build_audit_message("2026-09-18", to_remove, to_flag, n_subs=0, category_health={})

    assert "Auto-removed: 12 rows" in text  # true count preserved
    assert text.count("Product ") == 8
    assert "…and 4 more" in text

    assert "Flagged for review: 7 rows" in text
    assert text.count("Flag ") == 5
    assert "…and 2 more" in text


def test_build_message_include_health_false_omits_category_health_block():
    text = _build_audit_message("2026-09-18", [], [], n_subs=0,
                                 category_health={"Jewelry": 50}, include_health=False)
    assert "Category Health" not in text
    assert "Auto-removed: 0 rows" in text
    assert "Flagged for review: 0 rows" in text
    assert "Categories needing discovery: 0" in text


def test_build_message_include_health_true_lists_categories_by_score_desc():
    text = _build_audit_message("2026-09-18", [], [], n_subs=0,
                                 category_health={"Jewelry": 40, "Precious Metals": 90})
    assert text.index("Precious Metals") < text.index("Jewelry")


def test_build_message_empty_lists_have_no_stray_bullets():
    text = _build_audit_message("2026-09-18", [], [], n_subs=0, category_health={})
    assert "•" not in text
    assert "Auto-removed: 0 rows" in text


def test_build_message_review_hint_only_when_flagged():
    no_flag_text = _build_audit_message("2026-09-18", [], [], n_subs=0, category_health={})
    assert "AUDIT_REVIEW" not in no_flag_text

    with_flag = [(1, _product("X"), "reason")]
    flag_text = _build_audit_message("2026-09-18", [], with_flag, n_subs=0, category_health={})
    assert "Filter col A = AUDIT_REVIEW" in flag_text


# ── _remove_reason: SCORED $1–$4 dead-zone sweep ─────────────────────────────

from agents.auditor import (
    _remove_reason, _days_since,
    SCORED_STALE_NET_CEILING, SCORED_STALE_NET_FLOOR, SCORED_STALE_DAYS,
)


def test_stale_scored_dead_zone_is_removed_with_reason():
    reason = _remove_reason("SCORED", 2.50, 12, 45)
    assert reason == "Below $4 review floor, stale 45 days"


def test_dead_zone_net_boundaries():
    assert _remove_reason("SCORED", 1.00, 12, 30) is not None   # floor inclusive
    assert _remove_reason("SCORED", 3.99, 12, 30) is not None
    assert _remove_reason("SCORED", 4.00, 12, 30) is None       # ceiling exclusive
    # $0.99 is not this rule's job (borderline-flag path), and is >= $0.50
    assert _remove_reason("SCORED", 0.99, 12, 30) is None


def test_dead_zone_day_boundary():
    assert _remove_reason("SCORED", 2.50, 12, SCORED_STALE_DAYS - 1) is None
    assert _remove_reason("SCORED", 2.50, 12, SCORED_STALE_DAYS) is not None


def test_dead_zone_only_applies_to_scored():
    for status in ("WATCH", "PENDING", "PAUSED_MARGIN"):
        assert _remove_reason(status, 2.50, 12, 45) is None


def test_dead_zone_unknown_timestamp_is_not_treated_as_stale():
    assert _days_since("") == 999
    assert _remove_reason("SCORED", 2.50, 12, _days_since("")) is None


def test_zero_velocity_rule_keeps_precedence_no_double_catch():
    reason = _remove_reason("SCORED", 2.50, 0, 45)
    assert reason.startswith("Zero velocity")
    assert "review floor" not in reason


def test_existing_rules_unchanged():
    assert _remove_reason("SCORED", -1.0, 5, 1).startswith("Negative net profit")
    assert _remove_reason("SCORED", 0.20, 5, 1).startswith("Below floor")
    assert _remove_reason("PAUSED_OOS", 9.0, 5, 45) == "OOS 45+ days, no restock"
    assert _remove_reason("PENDING", 9.0, 5, 60) == "Stale 60 days (no progress)"
    assert _remove_reason("SCORED", 9.0, 5, 200) is None
    assert _remove_reason("SCORED", 9.0, 5, 1, "x wrong_product_flag y").startswith("Wrong product")


def test_sweep_ceiling_matches_review_floor():
    from agents.telegram_bot import _REVIEW_MIN_NET_PROFIT
    assert SCORED_STALE_NET_CEILING == _REVIEW_MIN_NET_PROFIT
    assert SCORED_STALE_NET_FLOOR == 1.00


# ── blank net = un-priced, NOT $0.00 (regression: 2026-09-23 audit removed 133
#    freshly discovered PENDING rows as "Below floor — net $0.00") ─────────────

from agents.auditor import _parse_net


def test_parse_net_blank_and_errors_are_none():
    for raw in ("", "  ", "#VALUE!", "#REF!", "N/A", "abc", None):
        assert _parse_net(raw) is None
    assert _parse_net("$2.35") == 2.35
    assert _parse_net("-1,320.85") == -1320.85
    assert _parse_net("0") == 0.0          # a real $0 is still a real zero


def test_unpriced_pending_row_is_not_removed_by_economics_rules():
    assert _remove_reason("PENDING", None, 0, 1) is None


def test_unpriced_row_still_removed_by_age_and_wrong_product_rules():
    assert _remove_reason("PENDING", None, 0, 60) == "Stale 60 days (no progress)"
    assert _remove_reason("PENDING", None, 0, 1, "wrong_product_flag").startswith("Wrong product")


def test_real_zero_net_still_removed():
    assert _remove_reason("SCORED", 0.0, 5, 1).startswith("Below floor")
