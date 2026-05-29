"""Unit tests for telegram_bot.py pure helpers."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.telegram_bot import (
    cookie_age_days,
    extract_last_timestamp,
    has_errors,
    parse_logs_arg,
    read_tail,
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
