# Telegram Status Bot — Design Spec
Date: 2026-05-28

## Overview

A persistent Telegram bot that runs as a systemd service on the Hermes VPS. It polls for incoming messages and responds to status/log commands. Gives Jordan visibility into agent health without SSH-ing into the VPS.

## Files Created

| File | Purpose |
|------|---------|
| `agents/telegram_bot.py` | Bot process — polling loop + command handlers |
| `deploy/telegram-bot.service` | Systemd unit file for the VPS |

`requirements.txt` gets one new line: `python-telegram-bot>=20.0`

## Architecture

Single standalone script. Uses `python-telegram-bot` v20+ in async polling mode. No webhook, no port, no nginx. Reads log files from disk on demand; no database, no shared state.

Startup sequence:
1. Load `.env` (same `load_dotenv` pattern as scheduler.py)
2. Validate `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set and `TELEGRAM_CHAT_ID` is numeric — exit with error if either check fails
3. Register command handlers
4. Call `Application.run_polling()` — runs forever

## Auth

Every handler checks `update.effective_user.id == int(TELEGRAM_CHAT_ID)`. Messages from any other sender are silently ignored (logged at DEBUG, no reply sent). This prevents the bot from leaking data if someone else discovers the bot username.

## Constants

```python
LOG_DIR = "/home/hermes/logs"
LOG_FILES = {
    "daily":    "/home/hermes/logs/daily.log",
    "rotation": "/home/hermes/logs/rotation.log",
    "sync":     "/home/hermes/logs/sync.log",
}
COOKIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "costco_cookies.json"
)
```

## Commands

### `/help`
Static reply listing all three commands with a one-line description each.

### `/status`
Reads last 20 lines of each log file. For each log:
- **Last run time**: extracted from the most recent timestamp line (loguru format: `YYYY-MM-DD HH:MM:SS`)
- **Pass/fail**: scans tail for `ERROR` or `Traceback` — fail if found, pass otherwise
- **Cookie age**: `os.path.getmtime(COOKIES_PATH)` → days since modification

Reply format (one block per log + cookie line at bottom):
```
daily: OK | last run 2026-05-28 06:00
rotation: OK | last run 2026-05-27 02:00
sync: FAIL (ERROR found) | last run 2026-05-28 06:01

Cookies: 3 days old
```

If a log file doesn't exist: `daily.log: not found` (no crash).
If cookies file doesn't exist: `Cookies: not found`.

### `/logs [mode]`
- `mode` = `daily`, `rotation`, or `sync` → returns last 30 lines of that file
- No argument → returns last 10 lines of all 3 files, separated by headers
- Unknown argument → error reply listing valid modes

Lines are sent as a single `<pre>`-wrapped Telegram message (HTML parse mode, consistent with existing `_send_telegram` in scheduler.py).

If a file doesn't exist: `[daily.log] not found`.

## Error Handling

- Missing env vars: log + sys.exit(1) at startup
- Log file not found: reply with `"X.log: not found"` — never crash the handler
- File read errors: catch exception, reply with `"Error reading X.log: {e}"`
- Telegram API errors: python-telegram-bot handles retries internally via polling

## `deploy/telegram-bot.service`

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

`ExecStart` uses the venv python explicitly — safer than relying on PATH. If the VPS uses a different venv path, Jordan adjusts this one line.

## Out of Scope

- Webhook mode (requires open port + nginx config)
- Inline keyboards or multi-step conversations
- `/run` command to trigger agent modes
- Any persistent state between restarts
