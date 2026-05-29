"""
Telegram Status Bot
===================
WAT Framework: Persistent Telegram bot for VPS agent monitoring.
Runs as a systemd service on Hermes VPS. Responds to /help, /status,
and /logs commands from the authorized TELEGRAM_CHAT_ID only.
"""

import os
import re
import sys
import time

from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)


# ── Constants ─────────────────────────────────────────────────────────────────

LOG_FILES = {
    "daily":    "/home/hermes/logs/daily.log",
    "rotation": "/home/hermes/logs/rotation.log",
    "sync":     "/home/hermes/logs/sync.log",
}

COOKIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "costco_cookies.json"
)

VALID_MODES = set(LOG_FILES.keys())

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_tail(path, n):
    """Return last n lines of path as a list, or None if file missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-n:] if len(lines) >= n else lines
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning(f"read_tail({path}): {e}")
        return None


def extract_last_timestamp(lines):
    """Return the most recent loguru-format timestamp string from lines, or None."""
    for line in reversed(lines):
        m = _TS_RE.search(line)
        if m:
            return m.group(1)
    return None


def has_errors(lines):
    """Return True if any line contains ERROR or Traceback."""
    return any("ERROR" in line or "Traceback" in line for line in lines)


def cookie_age_days(path):
    """Stub — implemented in Task 3."""
    raise NotImplementedError

def parse_logs_arg(text):
    """Stub — implemented in Task 3."""
    raise NotImplementedError
