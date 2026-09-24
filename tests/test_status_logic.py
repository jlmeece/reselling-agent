"""
Tests for tools/status_logic.py's pure status/staleness functions.

Pure functions — no I/O, no mocking needed.
"""
import sys
sys.path.insert(0, ".")

from datetime import datetime, timedelta

from tools.status_logic import days_since, check_scored_staleness, SCORED_STALE_DAYS


def _days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d %H:%M")


# ── days_since ───────────────────────────────────────────────────────────────

def test_days_since_parses_datetime_format():
    assert days_since(_days_ago(3)) == 3


def test_days_since_parses_date_only_format():
    date_only = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    assert days_since(date_only) == 10


def test_days_since_returns_999_when_blank():
    assert days_since("") == 999
    assert days_since(None) == 999


def test_days_since_returns_999_when_unparseable():
    assert days_since("not a date") == 999


# ── check_scored_staleness ────────────────────────────────────────────────────

def test_check_scored_staleness_no_change_under_threshold():
    new_status, notes = check_scored_staleness(_days_ago(SCORED_STALE_DAYS - 1))
    assert new_status is None
    assert notes is None


def test_check_scored_staleness_demotes_at_threshold():
    new_status, notes = check_scored_staleness(_days_ago(SCORED_STALE_DAYS))
    assert new_status == "PENDING"
    assert f"Stale ({SCORED_STALE_DAYS}d)" in notes
    assert "returned to PENDING for re-research" in notes


def test_check_scored_staleness_demotes_when_blank():
    new_status, notes = check_scored_staleness("")
    assert new_status == "PENDING"
    assert "Stale (999d)" in notes


# ── determine_status: margin is alert-only, stock is a hard pause ───────────────────────────────

from tools.status_logic import determine_status   # noqa: E402


def test_active_low_margin_stays_active_and_only_adds_a_note():
    status, code, notes = determine_status("ACTIVE", "In Stock", 0.04, False, 8, min_margin=0.10)
    assert status == "ACTIVE"                       # no auto-downgrade any more
    assert code == "ok"                             # nothing for the monitor to alert on
    assert "Margin 4.0% below 10% threshold" in notes


def test_active_negative_margin_still_only_a_note():
    status, code, notes = determine_status("ACTIVE", "In Stock", -0.20, False, 8, min_margin=0.10)
    assert (status, code) == ("ACTIVE", "ok") and "-20.0%" in notes


def test_active_healthy_margin_is_all_clear():
    assert determine_status("ACTIVE", "In Stock", 0.25, False, 8, min_margin=0.10) == ("ACTIVE", "ok", "All clear")


def test_active_unknown_margin_is_not_judged():
    status, code, notes = determine_status("ACTIVE", "In Stock", None, False, 8, min_margin=0.10)
    assert (status, code) == ("ACTIVE", "ok") and "Margin" not in notes


def test_oos_still_auto_pauses_and_low_margin_note_rides_along():
    status, code, notes = determine_status("ACTIVE", "OUT OF STOCK", 0.04, False, 8, min_margin=0.10)
    assert (status, code) == ("PAUSED_OOS", "oos")
    assert "OUT OF STOCK" in notes and "below 10% threshold" in notes


def test_active_never_becomes_paused_margin_for_any_margin():
    for margin in (None, -5.0, -0.01, 0.0, 0.099, 0.10, 0.5):
        for stock in ("In Stock", "Limited", "CHECK FAILED", "Available (limited)"):
            assert determine_status("ACTIVE", stock, margin, True, 3, min_margin=0.10)[0] == "ACTIVE"


def test_manual_paused_margin_recovers_to_watch():
    status, code, notes = determine_status("PAUSED_MARGIN", "In Stock", 0.18, False, None, min_margin=0.10)
    assert (status, code) == ("WATCH", "margin_recovered") and "18.0%" in notes


def test_manual_paused_margin_holds_while_margin_is_still_low_or_unknown():
    status, code, notes = determine_status("PAUSED_MARGIN", "In Stock", 0.04, False, None, min_margin=0.10)
    assert (status, code) == ("PAUSED_MARGIN", "ok") and "holding PAUSED_MARGIN" in notes
    assert determine_status("PAUSED_MARGIN", "In Stock", None, False, None)[0] == "PAUSED_MARGIN"


def test_paused_oos_recovery_unchanged():
    assert determine_status("PAUSED_OOS", "In Stock", None, False, None)[:2] == ("WATCH", "restock")
    assert determine_status("PAUSED_OOS", "OUT OF STOCK", None, False, None)[0] == "PAUSED_OOS"
