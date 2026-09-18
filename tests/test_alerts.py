"""
Tests for the crash-alert (agents/scheduler.py) and expired-cookie alert
(tools/costco_scraper.py) Telegram notification paths.

Fully mocked — no real network calls, no real Chrome, no real Google Sheets.
"""
import sys, os, json, time
sys.path.insert(0, ".")

import pytest


# ── scheduler.py: _send_telegram ────────────────────────────────────────────

def test_scheduler_send_telegram_posts_expected_payload(monkeypatch):
    from agents import scheduler

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", fake_urlopen)

    scheduler._send_telegram("tok123", "chat456", "hello world")

    assert captured["url"] == "https://api.telegram.org/bottok123/sendMessage"
    assert captured["body"] == {"chat_id": "chat456", "text": "hello world", "parse_mode": "HTML"}


def test_scheduler_send_telegram_never_raises_on_failure(monkeypatch):
    from agents import scheduler

    def fake_urlopen(req, timeout=None):
        raise ConnectionError("network down")

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", fake_urlopen)

    # Should not raise even though the network call fails.
    scheduler._send_telegram("tok", "chat", "text")


# ── scheduler.py: main() crash path ─────────────────────────────────────────

def test_scheduler_main_sends_crash_alert_before_reraising(monkeypatch, tmp_path):
    from agents import scheduler

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat456")
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--mode", "research"])

    # Isolate from real project state — no real lock file, sheet, or config I/O.
    monkeypatch.setattr(scheduler, "LOCK_FILE", str(tmp_path / ".scheduler_lock"))
    monkeypatch.setattr(scheduler, "_check_cookie_age", lambda: None)
    monkeypatch.setattr(scheduler, "load_config", lambda: {
        "business": {"sheet_name": "Tracker", "data_start_row": 4, "data_end_row": 500}
    })
    monkeypatch.setattr(scheduler, "load_col_map", lambda: {})
    monkeypatch.setattr(scheduler, "log_run_start", lambda mode: time.time())
    monkeypatch.setattr(scheduler, "log_run_end", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "send_run_summary", lambda *a, **k: None)

    def boom():
        raise RuntimeError("Google Sheets auth failed: bad credentials")

    monkeypatch.setattr(scheduler, "get_sheets_service", boom)

    sent = {}
    monkeypatch.setattr(scheduler, "_send_telegram",
                         lambda token, chat_id, text: sent.update(token=token, chat_id=chat_id, text=text))

    with pytest.raises(RuntimeError):
        scheduler.main()

    assert sent["token"] == "tok123"
    assert sent["chat_id"] == "chat456"
    assert sent["text"].startswith("💥 Scheduler [research] CRASHED — ")
    assert "Google Sheets auth failed" in sent["text"]


def test_scheduler_main_skips_alert_when_telegram_unconfigured(monkeypatch, tmp_path):
    from agents import scheduler

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--mode", "daily"])

    monkeypatch.setattr(scheduler, "LOCK_FILE", str(tmp_path / ".scheduler_lock"))
    monkeypatch.setattr(scheduler, "_check_cookie_age", lambda: None)
    monkeypatch.setattr(scheduler, "load_config", lambda: {
        "business": {"sheet_name": "Tracker", "data_start_row": 4, "data_end_row": 500}
    })
    monkeypatch.setattr(scheduler, "load_col_map", lambda: {})
    monkeypatch.setattr(scheduler, "log_run_start", lambda mode: time.time())
    monkeypatch.setattr(scheduler, "log_run_end", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "send_run_summary", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "get_sheets_service", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    called = []
    monkeypatch.setattr(scheduler, "_send_telegram", lambda *a, **k: called.append(a))

    with pytest.raises(RuntimeError):
        scheduler.main()

    assert called == []


# ── costco_scraper.py: expired-cookie alert ─────────────────────────────────

def _write_cookies(path, total, expired):
    """Writes `total` cookies, the first `expired` of which have a past expiry."""
    now = time.time()
    rows = []
    for i in range(total):
        exp = now - 3600 if i < expired else now + 86400 * 30
        rows.append({"name": f"c{i}", "value": "v", "domain": ".costco.com",
                      "path": "/", "expirationDate": exp})
    with open(path, "w") as f:
        json.dump(rows, f)


def test_costco_scraper_send_telegram_posts_expected_payload(monkeypatch):
    from tools import costco_scraper

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())

    monkeypatch.setattr(costco_scraper.urllib.request, "urlopen", fake_urlopen)

    costco_scraper._send_telegram("tok", "chat", "msg")

    assert captured["url"] == "https://api.telegram.org/bottok/sendMessage"
    assert captured["body"] == {"chat_id": "chat", "text": "msg", "parse_mode": "HTML"}


def test_load_cookies_alerts_when_over_20_percent_expired(monkeypatch, tmp_path):
    from tools import costco_scraper

    cookies_path = tmp_path / "costco_cookies.json"
    _write_cookies(cookies_path, total=20, expired=5)  # 25% expired, >20%
    monkeypatch.setattr(costco_scraper, "COOKIES_PATH", str(cookies_path))
    monkeypatch.setattr(costco_scraper, "_COOKIE_EXPIRY_ALERT_TS_PATH", str(tmp_path / ".alert_ts"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    sent = {}
    monkeypatch.setattr(costco_scraper, "_send_telegram",
                         lambda token, chat_id, text: sent.update(token=token, chat_id=chat_id, text=text))

    costco_scraper._load_cookies()

    assert sent["text"].startswith("⚠️ Costco cookies mostly expired (5/20 expired)")


def test_load_cookies_alerts_when_over_10_expired_even_under_20_percent(monkeypatch, tmp_path):
    from tools import costco_scraper

    cookies_path = tmp_path / "costco_cookies.json"
    _write_cookies(cookies_path, total=100, expired=11)  # 11% (<20%) but >10 absolute
    monkeypatch.setattr(costco_scraper, "COOKIES_PATH", str(cookies_path))
    monkeypatch.setattr(costco_scraper, "_COOKIE_EXPIRY_ALERT_TS_PATH", str(tmp_path / ".alert_ts"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    sent = {}
    monkeypatch.setattr(costco_scraper, "_send_telegram",
                         lambda token, chat_id, text: sent.update(text=text))

    costco_scraper._load_cookies()

    assert "11/100 expired" in sent["text"]


def test_load_cookies_no_alert_below_thresholds(monkeypatch, tmp_path):
    from tools import costco_scraper

    cookies_path = tmp_path / "costco_cookies.json"
    _write_cookies(cookies_path, total=100, expired=5)  # 5% and <10 absolute
    monkeypatch.setattr(costco_scraper, "COOKIES_PATH", str(cookies_path))
    monkeypatch.setattr(costco_scraper, "_COOKIE_EXPIRY_ALERT_TS_PATH", str(tmp_path / ".alert_ts"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    called = []
    monkeypatch.setattr(costco_scraper, "_send_telegram", lambda *a, **k: called.append(a))

    costco_scraper._load_cookies()

    assert called == []


def test_load_cookies_alert_throttled_within_24h(monkeypatch, tmp_path):
    from tools import costco_scraper

    cookies_path = tmp_path / "costco_cookies.json"
    _write_cookies(cookies_path, total=20, expired=15)
    monkeypatch.setattr(costco_scraper, "COOKIES_PATH", str(cookies_path))
    monkeypatch.setattr(costco_scraper, "_COOKIE_EXPIRY_ALERT_TS_PATH", str(tmp_path / ".alert_ts"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    call_count = {"n": 0}
    monkeypatch.setattr(costco_scraper, "_send_telegram", lambda *a, **k: call_count.__setitem__("n", call_count["n"] + 1))

    costco_scraper._load_cookies()  # first call — should alert
    costco_scraper._load_cookies()  # second call, same process — should be throttled

    assert call_count["n"] == 1


def test_load_cookies_alert_skipped_when_telegram_unconfigured(monkeypatch, tmp_path):
    from tools import costco_scraper

    cookies_path = tmp_path / "costco_cookies.json"
    _write_cookies(cookies_path, total=20, expired=15)
    monkeypatch.setattr(costco_scraper, "COOKIES_PATH", str(cookies_path))
    monkeypatch.setattr(costco_scraper, "_COOKIE_EXPIRY_ALERT_TS_PATH", str(tmp_path / ".alert_ts"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    called = []
    monkeypatch.setattr(costco_scraper, "_send_telegram", lambda *a, **k: called.append(a))

    # Should not raise, should log a warning instead (not asserted here — just no crash/no send).
    costco_scraper._load_cookies()

    assert called == []
