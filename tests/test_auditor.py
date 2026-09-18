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
