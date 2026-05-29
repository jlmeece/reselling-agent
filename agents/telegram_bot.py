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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
