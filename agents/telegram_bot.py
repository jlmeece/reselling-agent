"""
Telegram Status Bot
===================
WAT Framework: Persistent Telegram bot for agent monitoring. Runs as a
systemd service on the Hermes VPS or as a Windows Startup-folder process.
Responds to /help, /status, /logs, /lookup, and /dashboard commands from
the authorized TELEGRAM_CHAT_ID only.
"""

import html
import os
import re
import sys
import time
from datetime import datetime

import yaml
from dotenv import load_dotenv
from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import get_sheets_service, read_sheet


# ── Constants ─────────────────────────────────────────────────────────────────

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOG_FILES = {
    "active":        os.path.join(_BASE_DIR, "data", "logs", "active.log"),
    "audit":         os.path.join(_BASE_DIR, "data", "logs", "audit.log"),
    "daily":         os.path.join(_BASE_DIR, "data", "logs", "daily.log"),
    "research":      os.path.join(_BASE_DIR, "data", "logs", "research.log"),
    "rotation":      os.path.join(_BASE_DIR, "data", "logs", "rotation.log"),
    "discovery":     os.path.join(_BASE_DIR, "data", "logs", "discovery.log"),
    "refresh-notes": os.path.join(_BASE_DIR, "data", "logs", "refresh-notes.log"),
    "recheck":       os.path.join(_BASE_DIR, "data", "logs", "recheck.log"),
}

COOKIES_PATH = os.path.join(_BASE_DIR, "data", "costco_cookies.json")

VALID_MODES = set(LOG_FILES.keys())

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

_MAX_MSG = 4096


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


# ── Sheet config / lookup helpers ───────────────────────────────────────────

def col_to_idx(col_letter):
    """Convert column letter to 0-based index: 'A'->0, 'B'->1, ..., 'Z'->25, 'AA'->26."""
    col_letter = col_letter.upper()
    result = 0
    for ch in col_letter:
        result = result * 26 + (ord(ch) - ord('A') + 1)
    return result - 1


def safe_get(lst, i, default=""):
    """Return lst[i] if in range, else default. Rows from Sheets are ragged."""
    return lst[i] if i < len(lst) else default


def _load_col_map():
    p = os.path.join(_BASE_DIR, "config", "col_map.yaml")
    with open(p) as f:
        return yaml.safe_load(f)["columns"]


def _load_business_cfg():
    p = os.path.join(_BASE_DIR, "config", "categories.yaml")
    with open(p) as f:
        return yaml.safe_load(f)["business"]


def search_products(rows, col_map, term):
    """
    Case-insensitive substring match against the title OR category columns.
    rows: ragged list[list[str]] as returned by tools.sheet_writer.read_sheet.
    col_map: {field_name: "COLUMN_LETTER"} — config/col_map.yaml's "columns" dict.
    Returns a list of dicts (one per matching row) with the raw field strings
    needed for rendering. Never raises on ragged/short rows.
    """
    term_lc = term.strip().lower()
    if not term_lc:
        return []

    title_i = col_to_idx(col_map["title"])
    cat_i = col_to_idx(col_map["category"])

    fields = (
        "status", "stock_status", "costco_cost", "ebay_price",
        "net_profit", "net_margin", "last_checked", "costco_url", "sale_info",
    )
    field_idx = {name: col_to_idx(col_map[name]) for name in fields}

    matches = []
    for row in rows:
        if not row:
            continue
        title = safe_get(row, title_i)
        category = safe_get(row, cat_i)
        if term_lc in title.lower() or term_lc in category.lower():
            match = {"title": title, "category": category}
            for name, idx in field_idx.items():
                match[name] = safe_get(row, idx)
            matches.append(match)
    return matches


def _format_price(raw):
    """Strip a leading '$' (caller adds its own); '—' when blank."""
    raw = (raw or "").strip()
    if not raw:
        return "—"
    return raw[1:] if raw.startswith("$") else raw


def _format_net_fragment(net_profit_raw, net_margin_raw):
    """
    Render 'net $45.20 (18%)' from the sheet's own pre-formatted net_profit/
    net_margin strings. Falls back to em-dashes when blank; never raises.
    Leaves a negative value's own leading '-' alone (e.g. "-$12.50" stays put
    rather than becoming "$-$12.50").
    """
    net = (net_profit_raw or "").strip()
    margin = (net_margin_raw or "").strip()
    net_str = net if net else "—"
    margin_str = margin if margin else "—"
    if net_str != "—" and not net_str.startswith("$") and not net_str.startswith("-$"):
        net_str = f"${net_str}"
    if margin_str != "—" and not margin_str.endswith("%"):
        margin_str = f"{margin_str}%"
    return f"net {net_str} ({margin_str})"


def _format_sale_line(sale_info_raw, now=None):
    """
    Return '🔥 Sale ends MM/DD/YY (Nd left)', or None if sale_info is blank
    or its expiry date can't be parsed. Reuses the exact regex/parsing
    convention from agents/scheduler.py's sale-expiry check.
    """
    sale_info = (sale_info_raw or "").strip()
    if not sale_info:
        return None
    now = now or datetime.now()
    exp_match = re.search(r'ends?\s+(\d{1,2}/\d{1,2}/\d{2,4})', sale_info, re.IGNORECASE)
    if not exp_match:
        return None
    exp_str = exp_match.group(1)
    exp_dt = None
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            exp_dt = datetime.strptime(exp_str, fmt).replace(hour=23, minute=59)
            break
        except ValueError:
            continue
    if exp_dt is None:
        return None
    hours_left = (exp_dt - now).total_seconds() / 3600
    days_left = max(0, round(hours_left / 24))
    return f"🔥 Sale ends {exp_str} ({days_left}d left)"


def _format_last_checked_line(last_checked_raw, now=None):
    """
    Return 'Last checked Nh ago'. last_checked is written as '%Y-%m-%d %H:%M'
    (agents/scheduler.py:89). Falls back to 'unknown' when missing/
    unparseable; appends a STALE warning when older than 12 hours.
    """
    raw = (last_checked_raw or "").strip()
    if not raw:
        return "Last checked unknown"
    try:
        checked_dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return "Last checked unknown"
    now = now or datetime.now()
    hours_ago = max(0, (now - checked_dt).total_seconds() / 3600)
    line = f"Last checked {hours_ago:.0f}h ago"
    if hours_ago > 12:
        line += " ⚠️ STALE — price may have changed"
    return line


def format_product_detail(p, now=None):
    """
    Render the single-match /lookup card as plain text — no parse_mode is
    used for /lookup (nothing here needs bold/<pre>), which sidesteps
    html.escape() entirely for every sheet-derived free-text field.
    """
    lines = [
        f"📦 {p.get('title') or '(untitled)'}",
        f"{p.get('category') or '—'} · {p.get('status') or '—'}",
        f"Costco ${_format_price(p.get('costco_cost'))} → eBay ${_format_price(p.get('ebay_price'))}"
        f"  ·  {_format_net_fragment(p.get('net_profit'), p.get('net_margin'))}",
        f"Stock: {p.get('stock_status') or '—'}",
    ]
    sale_line = _format_sale_line(p.get("sale_info"), now=now)
    if sale_line:
        lines.append(sale_line)
    lines.append(_format_last_checked_line(p.get("last_checked"), now=now))
    lines.append(p.get("costco_url") or "—")
    return "\n".join(lines)


def _lookup_summary_line(p):
    return f"• {p.get('title') or '(untitled)'} — {p.get('category') or '—'} · {p.get('status') or '—'}"


def format_lookup_reply(matches, term, now=None):
    """
    Build the full /lookup reply for any match count:
      0    -> "No products found matching '<term>'."
      1    -> format_product_detail() full card
      2-5  -> one summary line per match + "be more specific"
      >5   -> "Too many matches — be more specific."
    """
    if not matches:
        return f"No products found matching '{term}'."

    if len(matches) == 1:
        return format_product_detail(matches[0], now=now)

    if len(matches) <= 5:
        header = f"Found {len(matches)} matches for '{term}':"
        lines = [header] + [_lookup_summary_line(p) for p in matches]
        lines.append("Be more specific.")
        return "\n".join(lines)

    return "Too many matches — be more specific."


# ── Dashboard helpers ────────────────────────────────────────────────────────

# (label, emoji) per canonical status, in the display priority order.
_DASHBOARD_GROUPS = [
    ("ACTIVE", "Active", "🟢"),
    ("READY", "Ready", "✅"),
    ("APPROVED", "Approved", "🟣"),
    ("SCORED", "Scored", "🔵"),
    ("PENDING", "Pending", "⬜️"),
    ("AUDIT_REVIEW", "Audit Review", "🟡"),
]

_PAUSED_STATUSES = {"PAUSED_OOS", "PAUSED_MARGIN", "PAUSED_DEMAND", "PAUSED_SEASONAL"}

_STATUS_TO_GROUP = {raw: label for raw, label, _ in _DASHBOARD_GROUPS}


def count_statuses(rows):
    """
    Count non-empty column-A status values from a ragged list[list[str]]
    (rows as returned by tools.sheet_writer.read_sheet, column A only).
    Returns (counts: {display_label: int}, total: int).
    """
    counts = {}
    total = 0
    for row in rows:
        status = safe_get(row, 0).strip()
        if not status:
            continue
        total += 1
        if status in _PAUSED_STATUSES:
            label = "Paused"
        else:
            label = _STATUS_TO_GROUP.get(status, "Other")
        counts[label] = counts.get(label, 0) + 1
    return counts, total


def format_dashboard_reply(counts, total):
    """Render the /dashboard funnel summary in fixed priority order, skipping 0-count groups."""
    order = [label for _, label, _ in _DASHBOARD_GROUPS] + ["Paused", "Other"]
    emoji = {label: em for _, label, em in _DASHBOARD_GROUPS}
    emoji["Paused"] = "⏸️"
    emoji["Other"] = "⚪️"

    lines = ["📊 WAT Dashboard", ""]
    for label in order:
        n = counts.get(label, 0)
        if n == 0:
            continue
        lines.append(f"{emoji[label]} {label}: {n}")

    lines.append("")
    lines.append(f"Total tracked: {total}")
    lines.append("Last updated: just now")
    return "\n".join(lines)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _authorized(update, chat_id):
    cid = update.effective_chat.id if update.effective_chat else None
    if cid != chat_id:
        logger.debug(f"Ignored message from unauthorized chat {cid}")
        return False
    return True


# ── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_help(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return
    text = (
        "<b>WAT Reselling Agent — Commands</b>\n\n"
        "/status — last run time, pass/fail, cookie age\n"
        "/logs [mode] — recent log lines (modes: active, audit, daily, research, rotation, discovery, refresh-notes, recheck)\n"
        "/lookup &lt;term&gt; — search Product Tracker by title or category\n"
        "/dashboard — funnel summary (counts by status)\n"
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
        safe_lines = [html.escape(l) for l in lines]
        text = f"<pre>{mode}.log (last 30 lines):\n" + "\n".join(safe_lines) + "</pre>"
        if len(text) > _MAX_MSG:
            text = text[:_MAX_MSG - 30] + "\n[truncated]</pre>"
        await update.message.reply_text(text, parse_mode="HTML")
        return

    sections = []
    for m, path in LOG_FILES.items():
        lines = read_tail(path, 10)
        body = "\n".join(html.escape(l) for l in lines) if lines is not None else "not found"
        sections.append(f"--- {m}.log ---\n{body}")
    text = "<pre>" + "\n\n".join(sections) + "</pre>"
    if len(text) > _MAX_MSG:
        text = text[:_MAX_MSG - 30] + "\n[truncated]</pre>"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_lookup(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return

    term = " ".join(context.args).strip() if context.args else ""
    if not term:
        await update.message.reply_text("Usage: /lookup <search term>")
        return

    try:
        col_map = _load_col_map()
        cfg = _load_business_cfg()
        service = get_sheets_service()
        sheet_name = cfg["sheet_name"]
        start, end = cfg["data_start_row"], cfg["data_end_row"]
        rows = read_sheet(service, f"'{sheet_name}'!A{start}:BA{end}")
    except Exception as e:
        logger.warning(f"/lookup sheet read failed: {e}")
        await update.message.reply_text(
            "Couldn't reach the product sheet right now — try again in a bit."
        )
        return

    matches = search_products(rows, col_map, term)
    text = format_lookup_reply(matches, term)
    if len(text) > _MAX_MSG:
        text = text[:_MAX_MSG - 20] + "\n[truncated]"
    await update.message.reply_text(text)


async def cmd_dashboard(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return

    try:
        cfg = _load_business_cfg()
        service = get_sheets_service()
        sheet_name = cfg["sheet_name"]
        start, end = cfg["data_start_row"], cfg["data_end_row"]
        rows = read_sheet(service, f"'{sheet_name}'!A{start}:A{end}")
    except Exception as e:
        logger.warning(f"/dashboard sheet read failed: {e}")
        await update.message.reply_text(
            "Couldn't reach the product sheet right now — try again in a bit."
        )
        return

    counts, total = count_statuses(rows)
    text = format_dashboard_reply(counts, total)
    if len(text) > _MAX_MSG:
        text = text[:_MAX_MSG - 20] + "\n[truncated]"
    await update.message.reply_text(text)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logger.add(
        os.path.join(_BASE_DIR, "data", "logs", "telegram_bot.log"),
        rotation="10 MB",
        retention=3,
        encoding="utf-8",
    )

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
    app.add_handler(CommandHandler("lookup", cmd_lookup))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))

    while True:
        try:
            logger.info("Polling for messages...")
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            break  # run_polling() returned normally (e.g. clean shutdown) — don't loop forever
        except Exception:
            logger.exception("Bot crashed — restarting in 30s")
            time.sleep(30)


if __name__ == "__main__":
    main()
