"""Tests for the telegram bot's liveness file (consumed by watchdog.ps1)."""
import asyncio
import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.telegram_bot import _liveness_loop, _post_init, _touch_liveness_file


def _app(running=True, updater_running=True, updater=True):
    app = MagicMock()
    app.running = running
    app.updater = MagicMock(running=updater_running) if updater else None
    return app


def _run_ticks(app, path, ticks):
    """Run _liveness_loop for `ticks` iterations, then cancel via sleep raising."""
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= ticks:
            raise asyncio.CancelledError

    async def go():
        with patch("agents.telegram_bot.asyncio.sleep", fake_sleep):
            try:
                await _liveness_loop(app, interval=60, path=path)
            except asyncio.CancelledError:
                pass

    asyncio.run(go())


def test_touch_creates_file(tmp_path):
    p = tmp_path / "alive"
    _touch_liveness_file(str(p))
    assert p.exists()


def test_touch_refreshes_mtime(tmp_path):
    p = tmp_path / "alive"
    p.write_text("")
    old = time.time() - 3600
    os.utime(p, (old, old))
    _touch_liveness_file(str(p))
    assert p.stat().st_mtime > old + 3000


def test_touch_swallows_oserror(tmp_path):
    # a directory path can't be opened for append -> OSError, must not raise
    _touch_liveness_file(str(tmp_path))


def test_loop_touches_when_polling(tmp_path):
    p = tmp_path / "alive"
    _run_ticks(_app(), str(p), ticks=1)
    assert p.exists()


def test_loop_skips_touch_when_updater_not_running(tmp_path):
    p = tmp_path / "alive"
    _run_ticks(_app(updater_running=False), str(p), ticks=2)
    assert not p.exists()


def test_loop_skips_touch_when_application_not_running(tmp_path):
    p = tmp_path / "alive"
    _run_ticks(_app(running=False), str(p), ticks=2)
    assert not p.exists()


def test_loop_skips_touch_when_no_updater(tmp_path):
    p = tmp_path / "alive"
    _run_ticks(_app(updater=False), str(p), ticks=2)
    assert not p.exists()


def test_loop_survives_touch_failure(tmp_path):
    # directory as path -> _touch swallows OSError; loop must keep ticking
    _run_ticks(_app(), str(tmp_path), ticks=3)


def test_post_init_starts_liveness_task_and_sends_notice(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.telegram_bot.LIVENESS_FILE", str(tmp_path / "alive"))
    app = _app()
    app.bot.send_message = MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0))
    app.bot_data = {"chat_id": 1}

    async def go():
        await _post_init(app)
        task = app.bot_data["_liveness_task"]
        assert not task.done()
        task.cancel()

    asyncio.run(go())
    app.bot.send_message.assert_called_once()
