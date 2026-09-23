"""Fast, fully-mocked tests for the scheduler's expiry-based cookie auto-refresh trigger."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import scheduler
from tools import cookie_refresh


def _cookies(n_total, n_expired, extra=()):
    now = time.time()
    out = []
    for i in range(n_total):
        exp = now - 3600 if i < n_expired else now + 86400
        out.append({"name": f"c{i}", "value": "v", "expirationDate": exp})
    return out + list(extra)


@pytest.fixture
def cookie_file(tmp_path, monkeypatch):
    path = tmp_path / "costco_cookies.json"
    monkeypatch.setattr(scheduler, "_COOKIES_PATH", str(path))
    monkeypatch.setattr(scheduler, "_COOKIE_WARN_TS_PATH", str(tmp_path / ".warn_ts"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def write(cookies, age_days=1):
        path.write_text(json.dumps(cookies))
        mtime = time.time() - age_days * 86400
        os.utime(path, (mtime, mtime))
    return write


@pytest.fixture
def refresh_calls(monkeypatch):
    calls = []

    def fake(reason="age"):
        calls.append(reason)
        return True, ""
    monkeypatch.setattr(scheduler, "_attempt_cookie_autorefresh", fake)
    return calls


def test_young_file_with_over_20pct_expired_triggers_refresh(cookie_file, refresh_calls):
    cookie_file(_cookies(10, 3), age_days=1)
    scheduler._check_cookie_age()
    assert refresh_calls == ["expiry"]


def test_young_file_with_under_20pct_expired_does_not_refresh(cookie_file, refresh_calls):
    cookie_file(_cookies(20, 2), age_days=1)
    scheduler._check_cookie_age()
    assert refresh_calls == []


def test_exactly_20pct_is_not_over_threshold(cookie_file, refresh_calls):
    cookie_file(_cookies(10, 2), age_days=1)
    scheduler._check_cookie_age()
    assert refresh_calls == []


def test_old_file_with_no_expired_cookies_still_refreshes_via_age(cookie_file, refresh_calls):
    cookie_file(_cookies(10, 0), age_days=30)
    scheduler._check_cookie_age()
    assert refresh_calls == ["age"]


def test_session_cookies_without_expiry_are_not_counted_expired(cookie_file, refresh_calls):
    session = [{"name": f"s{i}", "value": "v"} for i in range(10)]
    cookie_file(_cookies(10, 2, extra=session), age_days=1)  # 2/20 = 10%
    scheduler._check_cookie_age()
    assert refresh_calls == []


def test_missing_file_is_a_noop(cookie_file, refresh_calls):
    scheduler._check_cookie_age()
    assert refresh_calls == []


def test_corrupt_file_does_not_crash_or_refresh(cookie_file, refresh_calls):
    cookie_file([], age_days=1)
    open(scheduler._COOKIES_PATH, "w").write("{not json")
    scheduler._check_cookie_age()
    assert refresh_calls == []


def test_failed_refresh_falls_through_to_one_manual_warning_naming_expiry(cookie_file, monkeypatch):
    cookie_file(_cookies(10, 5), age_days=1)
    monkeypatch.setattr(scheduler, "_attempt_cookie_autorefresh", lambda reason="age": (False, "chrome closed"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = []
    monkeypatch.setattr(scheduler, "_send_telegram", lambda token, chat, text: sent.append(text))

    scheduler._check_cookie_age()
    scheduler._check_cookie_age()  # 7-day warn throttle: no second message

    assert len(sent) == 1
    assert "5/10 Costco cookies are expired" in sent[0]
    assert "chrome closed" in sent[0]


def test_throttled_refresh_does_not_crash(cookie_file, monkeypatch):
    """refresh_costco_cookies() returns (False, "") when inside the 24h throttle."""
    cookie_file(_cookies(10, 5), age_days=1)
    monkeypatch.setattr(scheduler, "refresh_costco_cookies", lambda: (False, ""))
    scheduler._check_cookie_age()  # must not raise


def test_expiry_stats_and_rule_match_scraper(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(_cookies(10, 3) + [{"name": "", "value": "x", "expirationDate": 1}]))
    assert cookie_refresh.cookie_expiry_stats(str(path)) == (3, 10)
    assert cookie_refresh.expiry_refresh_needed(3, 10)
    assert not cookie_refresh.expiry_refresh_needed(2, 10)
    assert cookie_refresh.expiry_refresh_needed(11, 100)   # >10 absolute
    assert not cookie_refresh.expiry_refresh_needed(0, 0)


# ── refresh_costco_cookies(): a fresh export that is still expired is not success ──

@pytest.fixture
def refresh_env(tmp_path, monkeypatch):
    """Point cookie_refresh at tmp files; fake subprocess.run so 'export' rewrites the cookie file."""
    path = tmp_path / "costco_cookies.json"
    monkeypatch.setattr(cookie_refresh, "_COOKIES_PATH", str(path))
    monkeypatch.setattr(cookie_refresh, "_COOKIE_AUTOREFRESH_TS_PATH", str(tmp_path / ".ar_ts"))
    path.write_text(json.dumps(_cookies(10, 8)))
    old = time.time() - 86400
    os.utime(path, (old, old))

    state = {"exported": None, "calls": []}

    def fake_run(cmd, **kwargs):
        script = os.path.basename(cmd[1])
        state["calls"].append(script)
        if script == "setup_costco_session.py":
            path.write_text(json.dumps(state["exported"]))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(cookie_refresh.subprocess, "run", fake_run)
    return state


def test_refresh_still_expired_after_export_fails_and_skips_upload(refresh_env):
    refresh_env["exported"] = _cookies(10, 8)
    ok, diag = cookie_refresh.refresh_costco_cookies()
    assert ok is False
    assert "still 8/10 cookies expired" in diag and "re-authenticate Chrome" in diag
    assert "cookie_sync.py" not in refresh_env["calls"]


def test_refresh_with_no_expired_cookies_succeeds_and_uploads(refresh_env):
    refresh_env["exported"] = _cookies(10, 0)
    assert cookie_refresh.refresh_costco_cookies() == (True, "")
    assert refresh_env["calls"] == ["setup_costco_session.py", "cookie_sync.py"]


def test_refresh_with_reduced_expired_count_succeeds(refresh_env):
    refresh_env["exported"] = _cookies(10, 2)  # 8 -> 2
    assert cookie_refresh.refresh_costco_cookies() == (True, "")
