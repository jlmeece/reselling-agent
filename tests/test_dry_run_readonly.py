"""--dry-run is read-only end to end (audit 10/21): no Run Log row, no heartbeat ping, and
ebay_sync / apply_prices print their Telegram text instead of sending it."""
import sys

import pytest

from agents import scheduler as sch
from tools import ebay_sync


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(sch, "_send_telegram", lambda tok, chat, text: calls.append(text) or True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    return calls


def test_ebay_sync_dry_run_prints_alert_instead_of_sending(monkeypatch, sent, capsys):
    monkeypatch.setattr(ebay_sync, "run_ebay_sync",
                        lambda *a, **k: {"status": "ok", "notes": "x", "alert": "🛒 losing money",
                                         "digest": "thin"})
    sch.run_ebay_sync_mode({}, {}, None, "Product Tracker", 4, 500, dry_run=True)
    assert sent == []
    out = capsys.readouterr().out
    assert "losing money" in out and "thin" in out


def test_ebay_sync_real_run_still_sends(monkeypatch, sent):
    monkeypatch.setattr(ebay_sync, "run_ebay_sync",
                        lambda *a, **k: {"status": "ok", "notes": "x", "alert": "A", "digest": None})
    sch.run_ebay_sync_mode({}, {}, None, "Product Tracker", 4, 500, dry_run=False)
    assert sent == ["A"]


def test_apply_prices_dry_run_prints_summary_instead_of_sending(monkeypatch, sent, capsys):
    monkeypatch.setattr(ebay_sync, "apply_target_prices", lambda *a, **k: {"results": []})
    monkeypatch.setattr(ebay_sync, "format_target_summary", lambda report: "💲 2 price changes")
    monkeypatch.setattr(ebay_sync, "summarize_targets", lambda report: "")
    sch.run_apply_prices_mode({}, {}, None, "Product Tracker", 4, 500, dry_run=True)
    assert sent == [] and "2 price changes" in capsys.readouterr().out


@pytest.mark.parametrize("dry", [True, False])
def test_main_dry_run_writes_no_run_log_and_no_heartbeat(monkeypatch, dry):
    logged, pinged = [], []
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--mode", "sale-digest"] + (["--dry-run"] if dry else []))
    monkeypatch.setattr(sch, "_acquire_lock", lambda mode: True)
    monkeypatch.setattr(sch, "_release_lock", lambda: None)
    monkeypatch.setattr(sch, "get_sheets_service", lambda: object())
    monkeypatch.setattr(sch, "run_sale_digest", lambda *a, **k: {"status": "ok", "notes": "n"})
    monkeypatch.setattr(sch, "log_run_end", lambda *a, **k: logged.append(a))
    monkeypatch.setattr(sch, "send_run_summary", lambda *a, **k: None)
    monkeypatch.setenv("HEALTHCHECK_URL_SALE_DIGEST", "https://hc-ping.example/x")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=5: pinged.append(url))
    sch.main()
    assert (logged == [] and pinged == []) if dry else (len(logged) == 1 and pinged)
