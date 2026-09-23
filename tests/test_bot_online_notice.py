"""Tests for the telegram bot's post_init online notice."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.telegram_bot import _notify_online


def _app(send):
    app = MagicMock()
    app.bot_data = {"chat_id": 12345}
    app.bot.send_message = send
    return app


def test_notify_online_sends_pid_to_authorized_chat():
    send = AsyncMock()
    asyncio.run(_notify_online(_app(send)))
    send.assert_awaited_once_with(12345, f"✅ Bot back online (PID {os.getpid()})")


def test_notify_online_swallows_send_failure():
    send = AsyncMock(side_effect=RuntimeError("network down"))
    asyncio.run(_notify_online(_app(send)))  # must not raise
    send.assert_awaited_once()
