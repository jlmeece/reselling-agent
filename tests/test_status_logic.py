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
