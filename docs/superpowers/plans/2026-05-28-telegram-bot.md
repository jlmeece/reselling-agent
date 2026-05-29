# Telegram Status Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a persistent Telegram bot that runs as a systemd service on the Hermes VPS and responds to `/status`, `/logs`, and `/help` commands with agent health information.

**Architecture:** A single standalone async script using `python-telegram-bot` v20+ in polling mode. Pure helper functions handle all log-reading and parsing logic (testable without Telegram), and command handlers call those helpers and reply. Auth guard on every handler silently drops messages from any sender other than `TELEGRAM_CHAT_ID`.

**Tech Stack:** `python-telegram-bot>=20.0`, `loguru`, `python-dotenv`, `pytest` (already available)

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `requirements.txt` | Modify | Add `python-telegram-bot>=20.0` |
| `agents/telegram_bot.py` | Create | Bot script — helpers + handlers + main() |
| `deploy/telegram-bot.service` | Create | Systemd unit file for VPS |
| `tests/test_telegram_bot.py` | Create | Unit tests for pure helper functions |

---

## Task 1: Add dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add python-telegram-bot to requirements.txt**

Open `requirements.txt` and append this line at the end:

```
python-telegram-bot>=20.0
```

- [ ] **Step 2: Commit**

```bash
git add requirements.txt
git commit -m "feat: add python-telegram-bot dependency"
```

---

## Task 2: TDD — log-reading helpers (read_tail, extract_last_timestamp, has_errors)

These three helpers are the core of `/status` and `/logs`. All pure functions — no Telegram dependency.

**Files:**
- Create: `tests/test_telegram_bot.py`
- Create: `agents/telegram_bot.py` (helpers only at this stage)

- [ ] **Step 1: Write failing tests for read_tail, extract_last_timestamp, has_errors**

Create `tests/test_telegram_bot.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail (ImportError expected)**

```bash
cd c:\Users\jorda\projects\reselling-agent
python -m pytest tests/test_telegram_bot.py::test_read_tail_returns_last_n_lines -v
```

Expected output: `ImportError` or `ModuleNotFoundError` — `telegram_bot.py` doesn't exist yet. This confirms the test is wired correctly.

- [ ] **Step 3: Create agents/telegram_bot.py with the three helpers**

Create `agents/telegram_bot.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_telegram_bot.py -k "read_tail or extract_last_timestamp or has_errors" -v
```

Expected output: all 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/telegram_bot.py tests/test_telegram_bot.py
git commit -m "feat: telegram_bot log-reading helpers with tests"
```

---

## Task 3: TDD — cookie_age_days and parse_logs_arg

**Files:**
- Modify: `tests/test_telegram_bot.py` — add new tests
- Modify: `agents/telegram_bot.py` — add two helpers

- [ ] **Step 1: Add failing tests to tests/test_telegram_bot.py**

Append to the bottom of `tests/test_telegram_bot.py`:

```python
# ── cookie_age_days ───────────────────────────────────────────────────────────

def test_cookie_age_days_returns_float(tmp_path):
    f = tmp_path / "cookies.json"
    f.write_text("{}")
    age = cookie_age_days(str(f))
    assert isinstance(age, float)
    assert age < 1.0  # just created


def test_cookie_age_days_missing_file_returns_none():
    age = cookie_age_days("/nonexistent/cookies.json")
    assert age is None


# ── parse_logs_arg ────────────────────────────────────────────────────────────

def test_parse_logs_arg_valid_modes():
    assert parse_logs_arg("daily") == ("daily", None)
    assert parse_logs_arg("rotation") == ("rotation", None)
    assert parse_logs_arg("sync") == ("sync", None)


def test_parse_logs_arg_empty_string():
    mode, err = parse_logs_arg("")
    assert mode is None
    assert err is None


def test_parse_logs_arg_none():
    mode, err = parse_logs_arg(None)
    assert mode is None
    assert err is None


def test_parse_logs_arg_whitespace():
    mode, err = parse_logs_arg("  ")
    assert mode is None
    assert err is None


def test_parse_logs_arg_unknown_mode():
    mode, err = parse_logs_arg("badmode")
    assert mode is None
    assert err is not None
    assert "badmode" in err


def test_parse_logs_arg_case_insensitive():
    mode, err = parse_logs_arg("DAILY")
    assert mode == "daily"
    assert err is None
```

- [ ] **Step 2: Run tests to verify new ones fail**

```bash
python -m pytest tests/test_telegram_bot.py -k "cookie_age or parse_logs" -v
```

Expected output: `ImportError` for `cookie_age_days` and `parse_logs_arg` (not defined yet).

- [ ] **Step 3: Add cookie_age_days and parse_logs_arg to agents/telegram_bot.py**

Append after the `has_errors` function in `agents/telegram_bot.py`:

```python
def cookie_age_days(path):
    """Return age of file in days, or None if file doesn't exist."""
    try:
        return (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return None


def parse_logs_arg(text):
    """
    Parse the argument string from /logs.
    Returns (mode, None) on valid input, (None, error_msg) on unknown mode,
    (None, None) if no argument given.
    """
    if not text or not text.strip():
        return None, None
    mode = text.strip().lower()
    if mode in VALID_MODES:
        return mode, None
    return None, f"Unknown mode '{mode}'. Valid: {', '.join(sorted(VALID_MODES))}"
```

- [ ] **Step 4: Run all tests to verify they pass**

```bash
python -m pytest tests/test_telegram_bot.py -v
```

Expected output: all 20 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/telegram_bot.py tests/test_telegram_bot.py
git commit -m "feat: add cookie_age_days and parse_logs_arg helpers with tests"
```

---

## Task 4: Implement bot handlers and main()

No unit tests for handlers (they require mocking the Telegram `Update` object — integration testing is done by running the bot). Add the Telegram imports, auth guard, three command handlers, and `main()` to the existing `agents/telegram_bot.py`.

**Files:**
- Modify: `agents/telegram_bot.py`

- [ ] **Step 1: Add Telegram imports at the top of agents/telegram_bot.py**

After the existing imports block (after `from loguru import logger`), add:

```python
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
```

- [ ] **Step 2: Add auth guard and command handlers**

Append the following after `parse_logs_arg` in `agents/telegram_bot.py`:

```python
# ── Auth ──────────────────────────────────────────────────────────────────────

def _authorized(update, chat_id):
    uid = update.effective_user.id if update.effective_user else None
    if uid != chat_id:
        logger.debug(f"Ignored message from unauthorized user {uid}")
        return False
    return True


# ── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_help(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return
    text = (
        "<b>WAT Reselling Agent — Commands</b>\n\n"
        "/status — last run time, pass/fail, cookie age\n"
        "/logs [mode] — recent log lines (modes: daily, rotation, sync)\n"
        "/help — this message"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_status(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return

    parts = []
    for mode, path in LOG_FILES.items():
        lines = read_tail(path, 20)
        if lines is None:
            parts.append(f"{mode}: not found")
            continue
        ts = extract_last_timestamp(lines) or "unknown"
        status = "FAIL (error found)" if has_errors(lines) else "OK"
        parts.append(f"{mode}: {status} | last run {ts}")

    age = cookie_age_days(COOKIES_PATH)
    cookie_line = f"Cookies: {age:.0f} days old" if age is not None else "Cookies: not found"
    parts.append(f"\n{cookie_line}")

    await update.message.reply_text("\n".join(parts))


async def cmd_logs(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return

    arg = " ".join(context.args) if context.args else None
    mode, err = parse_logs_arg(arg)

    if err:
        await update.message.reply_text(err)
        return

    if mode:
        lines = read_tail(LOG_FILES[mode], 30)
        if lines is None:
            await update.message.reply_text(f"{mode}.log: not found")
            return
        text = f"<pre>{mode}.log (last 30 lines):\n" + "\n".join(lines) + "</pre>"
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        sections = []
        for m, path in LOG_FILES.items():
            lines = read_tail(path, 10)
            body = "\n".join(lines) if lines is not None else "not found"
            sections.append(f"--- {m}.log ---\n{body}")
        text = "<pre>" + "\n\n".join(sections) + "</pre>"
        await update.message.reply_text(text, parse_mode="HTML")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env — exiting.")
        sys.exit(1)
    if not chat_id_raw:
        logger.error("TELEGRAM_CHAT_ID is not set in .env — exiting.")
        sys.exit(1)
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        logger.error(f"TELEGRAM_CHAT_ID must be a number, got: {chat_id_raw!r} — exiting.")
        sys.exit(1)

    logger.info(f"Telegram bot starting (authorized chat_id={chat_id})")

    app = Application.builder().token(token).build()
    app.bot_data["chat_id"] = chat_id

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))

    logger.info("Polling for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify existing tests still pass**

```bash
python -m pytest tests/test_telegram_bot.py -v
```

Expected output: all 20 tests PASS. (The Telegram imports are at the top of the module — if `python-telegram-bot` is not yet installed, this step will fail. Install it first: `pip install "python-telegram-bot>=20.0"`)

- [ ] **Step 4: Smoke test the bot locally**

Ensure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env`, then run:

```bash
python agents/telegram_bot.py
```

Expected output:
```
INFO | Telegram bot starting (authorized chat_id=<your_id>)
INFO | Polling for messages...
```

Send `/help` to the bot from Telegram. Expected reply:
```
WAT Reselling Agent — Commands

/status — last run time, pass/fail, cookie age
/logs [mode] — recent log lines (modes: daily, rotation, sync)
/help — this message
```

Stop the bot with Ctrl+C.

- [ ] **Step 5: Commit**

```bash
git add agents/telegram_bot.py
git commit -m "feat: add telegram bot handlers and main()"
```

---

## Task 5: Create systemd service file

**Files:**
- Create: `deploy/telegram-bot.service`

- [ ] **Step 1: Create deploy/ directory and service file**

Create `deploy/telegram-bot.service`:

```ini
[Unit]
Description=WAT Reselling Agent — Telegram Status Bot
After=network.target

[Service]
Type=simple
User=hermes
WorkingDirectory=/home/hermes/reselling-agent
ExecStart=/home/hermes/reselling-agent/venv/bin/python agents/telegram_bot.py
EnvironmentFile=/home/hermes/reselling-agent/.env
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Note: `ExecStart` uses the venv's Python explicitly. If the VPS venv is at a different path, update this line. The `EnvironmentFile` directive loads `.env` directly so no `source .env` wrapper is needed.

- [ ] **Step 2: Commit**

```bash
git add deploy/telegram-bot.service
git commit -m "feat: add systemd service unit for telegram bot"
```

---

## VPS Deployment (after pushing to VPS)

Once the code is on the VPS (`/home/hermes/reselling-agent`), run these commands once:

```bash
# Install the new dependency
cd /home/hermes/reselling-agent
source venv/bin/activate
pip install "python-telegram-bot>=20.0"

# Install and start the service
sudo cp deploy/telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot

# Verify it's running
sudo systemctl status telegram-bot
```

To tail the bot's live logs:
```bash
sudo journalctl -u telegram-bot -f
```
