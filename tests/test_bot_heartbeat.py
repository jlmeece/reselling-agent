"""Tests for the telegram bot's healthchecks.io heartbeat thread."""
import os
import sys
import threading
from unittest.mock import MagicMock, patch
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.telegram_bot import _heartbeat_loop, _start_heartbeat_thread

URL = "https://hc-ping.com/abc-123"


def _stop(*wait_results):
    stop = MagicMock()
    stop.wait.side_effect = list(wait_results)
    return stop


def test_heartbeat_pings_immediately_then_every_interval():
    stop = _stop(False, False, True)  # two full intervals elapse, then stop
    with patch("urllib.request.urlopen") as urlopen:
        _heartbeat_loop(URL, interval=300, stop=stop)
    assert urlopen.call_count == 3  # 1 at startup + 1 per elapsed interval
    urlopen.assert_called_with(URL, timeout=5)
    assert [c.args for c in stop.wait.call_args_list] == [(300,), (300,), (300,)]


def test_heartbeat_swallows_ping_failure_and_keeps_going():
    stop = _stop(False, True)
    with patch("urllib.request.urlopen", side_effect=URLError("network down")) as urlopen:
        _heartbeat_loop(URL, interval=300, stop=stop)  # must not raise
    assert urlopen.call_count == 2


def test_start_heartbeat_skips_when_env_unset(monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_URL_BOT", raising=False)
    with patch("agents.telegram_bot.threading.Thread") as thread_cls:
        assert _start_heartbeat_thread() is None
    thread_cls.assert_not_called()


def test_start_heartbeat_skips_when_env_blank(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_URL_BOT", "   ")
    with patch("agents.telegram_bot.threading.Thread") as thread_cls:
        assert _start_heartbeat_thread() is None
    thread_cls.assert_not_called()


def test_start_heartbeat_starts_daemon_thread_when_env_set(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_URL_BOT", f"  {URL}  ")
    with patch("agents.telegram_bot.threading.Thread") as thread_cls:
        result = _start_heartbeat_thread()
    thread_cls.assert_called_once()
    assert thread_cls.call_args.kwargs["target"] is _heartbeat_loop
    assert thread_cls.call_args.kwargs["args"] == (URL,)  # stripped
    assert thread_cls.call_args.kwargs["daemon"] is True
    thread_cls.return_value.start.assert_called_once()
    assert result is thread_cls.return_value


def test_heartbeat_real_daemon_thread_pings_repeatedly(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_URL_BOT", URL)
    stop = threading.Event()
    calls = []

    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        if len(calls) >= 3:
            stop.set()
        return MagicMock()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        t = threading.Thread(target=_heartbeat_loop, args=(URL, 0.01, stop), daemon=True)
        t.start()
        t.join(timeout=5)
    assert not t.is_alive()
    assert calls == [(URL, 5)] * 3
