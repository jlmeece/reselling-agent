"""
Telegram Status Bot
===================
WAT Framework: Persistent Telegram bot for agent monitoring. Runs as a
systemd service on the Hermes VPS or as a Windows Startup-folder process.
Responds to /help, /status, /logs, /lookup, and /dashboard commands from
the authorized TELEGRAM_CHAT_ID only.
"""

import asyncio
import html
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta

import psutil
import yaml
from dotenv import load_dotenv
from loguru import logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)

from tools.ebay_export import export_approved_products
from tools.graveyard_writer import write_to_graveyard
from tools.sheet_writer import get_sheets_service, read_sheet, write_row_partial
from tools.spot_price import get_spot_price, parse_gold_weight


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
    "telegram_bot":  os.path.join(_BASE_DIR, "data", "logs", "telegram_bot.log"),
}

COOKIES_PATH = os.path.join(_BASE_DIR, "data", "costco_cookies.json")
PID_FILE = os.path.join(_BASE_DIR, "data", ".telegram_bot.pid")

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


# ── PID lockfile ─────────────────────────────────────────────────────────────

def _is_duplicate_instance(old_pid, current_pid, pid_exists_fn):
    """
    True if old_pid names a *different*, currently-running process — i.e. a
    real second instance, not a stale leftover file and not ourselves.
    old_pid=None means no lockfile / unparseable content — never a duplicate.
    current_pid must be excluded because os.execv() (used by /restart) keeps
    the same PID across re-exec, and would otherwise find its own PID in the
    file and refuse to start.
    """
    if old_pid is None or old_pid == current_pid:
        return False
    return pid_exists_fn(old_pid)


def _read_pid_file(path):
    """Return the integer PID stored in path, or None if missing/unparseable."""
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _check_and_write_pid_lock():
    """
    Exit immediately if another instance is already running (per PID_FILE);
    otherwise write our own PID so a later launch can detect us in turn.
    """
    old_pid = _read_pid_file(PID_FILE)
    if _is_duplicate_instance(old_pid, os.getpid(), psutil.pid_exists):
        logger.info(f"Another instance already running (PID {old_pid}), exiting.")
        sys.exit(0)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


# ── Heartbeat (healthchecks.io) ─────────────────────────────────────────────

_HEARTBEAT_ENV = "HEALTHCHECK_URL_BOT"
_HEARTBEAT_INTERVAL = 300  # seconds between pings
_HEARTBEAT_TIMEOUT = 5     # seconds per ping


def _ping_healthcheck(url):
    """One healthchecks.io ping. Logs success/failure; never raises."""
    try:
        with urllib.request.urlopen(url, timeout=_HEARTBEAT_TIMEOUT):
            pass
        logger.info(f"Heartbeat ping sent ({_HEARTBEAT_ENV})")
    except Exception as e:
        logger.warning(f"Heartbeat ping FAILED ({_HEARTBEAT_ENV}): {e}")


def _heartbeat_loop(url, interval=_HEARTBEAT_INTERVAL, stop=None):
    """Ping immediately, then every `interval` seconds until `stop` (a
    threading.Event) is set. Runs on a daemon thread, so it dies with the process
    and the pings stop — that silence is what healthchecks.io alerts on."""
    stop = stop or threading.Event()
    while True:
        _ping_healthcheck(url)
        if stop.wait(interval):
            break


def _start_heartbeat_thread():
    """Start the heartbeat daemon thread if HEALTHCHECK_URL_BOT is set.
    Returns the thread, or None when the env var is empty/unset."""
    hc_url = os.getenv(_HEARTBEAT_ENV, "").strip()
    if not hc_url:
        logger.info(f"Heartbeat disabled ({_HEARTBEAT_ENV} not set)")
        return None
    thread = threading.Thread(target=_heartbeat_loop, args=(hc_url,), daemon=True, name="hc-heartbeat")
    thread.start()
    logger.info(f"Heartbeat thread started ({_HEARTBEAT_ENV} set, every {_HEARTBEAT_INTERVAL}s)")
    return thread


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


def _load_category_names():
    """Category display names in categories.yaml's declared order (matches col D)."""
    p = os.path.join(_BASE_DIR, "config", "categories.yaml")
    with open(p) as f:
        return list(yaml.safe_load(f)["categories"].keys())


_LOOKUP_PRODUCT_FIELDS = (
    "status", "stock_status", "costco_cost", "ebay_price",
    "net_profit", "net_margin", "last_checked", "costco_url", "sale_info",
    "fee_rate", "ebay_fees", "ship_cost", "ad_cost", "ad_budget",
    "regular_price", "demand_score",
)

# Matches config/categories.yaml's "business.data_start_row" — the first real
# product row in the Product Tracker sheet (rows 1-3 are header/legend). Only
# used as a fallback default so existing callers/tests that don't care about
# absolute row numbers don't have to pass it; real handlers pass the actual
# cfg value explicitly.
_DEFAULT_DATA_START_ROW = 4


def _extract_rows_by_field(rows, col_map, fields, *, data_start_row, filter_fn=None):
    """
    Shared row-extraction helper behind search_products/extract_dashboard_products
    and the Review/Audit queue extractors. Builds one dict per row that passes
    filter_fn (or every non-blank row if filter_fn is None), pulling `fields`
    out via col_map, plus the absolute 1-based sheet row number needed by any
    write-back action (Approve/Pause/Audit/Keep/Delete).

    rows: ragged list[list[str]] as returned by tools.sheet_writer.read_sheet,
    starting at data_start_row.
    filter_fn(row) -> bool: optional predicate over the raw row; callers close
    over whatever column indices they need. Never raises on ragged/short rows.
    """
    field_idx = {name: col_to_idx(col_map[name]) for name in fields}
    out = []
    for offset, row in enumerate(rows):
        if not row:
            continue
        if filter_fn and not filter_fn(row):
            continue
        item = {name: safe_get(row, idx) for name, idx in field_idx.items()}
        item["row_num"] = data_start_row + offset
        out.append(item)
    return out


def search_products(rows, col_map, term, data_start_row=_DEFAULT_DATA_START_ROW):
    """
    Case-insensitive substring match against the title OR category columns.
    rows: ragged list[list[str]] as returned by tools.sheet_writer.read_sheet.
    col_map: {field_name: "COLUMN_LETTER"} — config/col_map.yaml's "columns" dict.
    Returns a list of dicts (one per matching row) with the raw field strings
    needed for rendering, plus row_num (the absolute sheet row, for write-back
    actions). Never raises on ragged/short rows.
    """
    term_lc = term.strip().lower()
    if not term_lc:
        return []

    title_i = col_to_idx(col_map["title"])
    cat_i = col_to_idx(col_map["category"])

    def matches_term(row):
        title = safe_get(row, title_i)
        category = safe_get(row, cat_i)
        return term_lc in title.lower() or term_lc in category.lower()

    fields = _LOOKUP_PRODUCT_FIELDS + ("title", "category")
    return _extract_rows_by_field(
        rows, col_map, fields, data_start_row=data_start_row, filter_fn=matches_term
    )


def extract_review_queue(rows, col_map, data_start_row=_DEFAULT_DATA_START_ROW):
    """
    SCORED rows sorted by demand_score descending (unparseable scores sort
    last) — feeds the Telegram bot's swipe-style Review queue. Same field set
    as search_products, so a Review card renders via the same
    format_product_detail() body as a /lookup card.
    """
    status_i = col_to_idx(col_map["status"])

    def is_scored(row):
        return safe_get(row, status_i) == "SCORED"

    fields = _LOOKUP_PRODUCT_FIELDS + ("title", "category")
    items = _extract_rows_by_field(
        rows, col_map, fields, data_start_row=data_start_row, filter_fn=is_scored
    )

    def sort_key(p):
        score = _parse_currency(p["demand_score"])
        return -score if score is not None else float("inf")

    items.sort(key=sort_key)
    return items


def extract_audit_queue(rows, col_map, data_start_row=_DEFAULT_DATA_START_ROW):
    """
    AUDIT_REVIEW rows in sheet order (stable/deterministic — no urgency
    ordering is defined for this queue). Includes tier_summary (col T), where
    agents/auditor.py writes the human-readable flag reason, so the Audit
    card can show *why* a row was flagged.
    """
    status_i = col_to_idx(col_map["status"])

    def is_flagged(row):
        return safe_get(row, status_i) == "AUDIT_REVIEW"

    fields = _LOOKUP_PRODUCT_FIELDS + ("title", "category", "tier_summary")
    return _extract_rows_by_field(
        rows, col_map, fields, data_start_row=data_start_row, filter_fn=is_flagged
    )


def _format_price(raw):
    """Strip a leading '$' (caller adds its own); '—' when blank."""
    raw = (raw or "").strip()
    if not raw:
        return "—"
    return raw[1:] if raw.startswith("$") else raw


def _format_net_fragment(net_profit_raw, net_margin_raw, label="net"):
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
    return f"{label} {net_str} ({margin_str})"


def _tier_label(demand_score_raw):
    """
    Classify a demand_score cell into 'Tier 1 🥇' / 'Tier 2' / 'Tier 3' using
    col_map.yaml's documented thresholds (Tier1>=7, Tier2>=4, Tier3<4).
    Deliberately NOT ~/.claude/skills/base_scoring.py's assign_tier() — that
    shared skill uses different thresholds (6.0/3.0) tuned for a different
    project; this sheet's formulas and the business's own convention are
    authored against 7/4. Returns None if demand_score is blank/unparseable.
    """
    score = _parse_currency(demand_score_raw)
    if score is None:
        return None
    if score >= 7:
        return "Tier 1 🥇"
    if score >= 4:
        return "Tier 2"
    return "Tier 3"


def _format_fee_rate_pct(raw):
    """
    Render a fee_rate (col AB) cell as 'NN.N%'. Written as a raw Python float
    (e.g. 0.1325) with no PERCENT number format on the sheet, so it typically
    reads back as plain '0.1325' — but tolerate an already-'%'-suffixed cell
    too. Returns None if blank/unparseable.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.endswith("%"):
        return raw
    val = _parse_currency(raw)
    if val is None:
        return None
    pct = val * 100 if 0 < val < 1 else val
    return f"{pct:.1f}%"


def _format_regular_price_line(regular_price_raw, costco_cost_raw):
    """
    Render '🏷️ Was $X, now $Y (save $Z)' when regular_price (col AW) is
    populated and numerically differs from costco_cost — i.e. Costco's own
    pre-sale price vs the current price. Returns None when regular_price is
    blank or equals costco_cost. Uses 🏷️ deliberately, distinct from 🔥
    (reserved for the expiry countdown in _format_sale_line) — these are two
    different signals (price-delta vs time-remaining) that can coexist.
    """
    reg = _parse_currency(regular_price_raw)
    now_price = _parse_currency(costco_cost_raw)
    if reg is None or now_price is None or abs(reg - now_price) < 0.01:
        return None
    save = reg - now_price
    return f"🏷️ Was ${reg:,.2f}, now ${now_price:,.2f} (save ${save:,.2f})"


def _format_net_with_ads_line(net_profit_raw, ad_budget_raw, ebay_price_raw):
    """
    Recompute net profit/margin after subtracting the suggested ad_budget.
    Margin base mirrors col_map.yaml's net_margin formula (net / ebay_price).
    Guards ebay_price<=0 (no division by zero) and treats blank ad_budget as
    $0. Returns None if net_profit itself is unparseable.
    """
    net = _parse_currency(net_profit_raw)
    if net is None:
        return None
    ad = _parse_currency(ad_budget_raw) or 0.0
    price = _parse_currency(ebay_price_raw)
    net_with_ads = net - ad
    if price and price > 0:
        margin_str = f"{net_with_ads / price * 100:.0f}%"
    else:
        margin_str = "—"
    sign = "-" if net_with_ads < 0 else ""
    return f"Net with ads: {sign}${abs(net_with_ads):,.2f} ({margin_str})"


def _parse_sale_expiry_info(sale_info_raw, now=None):
    """
    Parse the 'ends MM/DD/YY' substring out of a sale_info cell (e.g.
    '🔥 -$150 ends 5/31/26'). Returns (exp_str, days_left, raw_days_left) or
    None if sale_info is blank or its expiry date can't be parsed. days_left
    is clamped to >=0 for display; raw_days_left is the true (possibly
    negative) signed value, used to detect and filter stale/expired sales.
    Reuses the exact regex/parsing convention from agents/scheduler.py's
    sale-expiry check. Shared by _format_sale_line (/lookup) and the
    /dashboard sale-urgency section.
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
    raw_days_left = round(hours_left / 24)
    days_left = max(0, raw_days_left)
    return exp_str, days_left, raw_days_left


def _format_sale_line(sale_info_raw, now=None):
    """
    Return '🔥 Sale ends MM/DD/YY (Nd left)', or '🔥 Sale ended MM/DD/YY
    (expired)' if the expiry date is in the past. None if sale_info is
    blank or its expiry date can't be parsed.
    """
    parsed = _parse_sale_expiry_info(sale_info_raw, now=now)
    if not parsed:
        return None
    exp_str, days_left, raw_days_left = parsed
    if raw_days_left < 0:
        return f"🔥 Sale ended {exp_str} (expired)"
    return f"🔥 Sale ends {exp_str} ({days_left}d left)"


def _hours_since_checked(last_checked_raw, now=None):
    """
    Hours since a last_checked cell ('%Y-%m-%d %H:%M', agents/scheduler.py:89),
    or None if blank/unparseable. Shared by _format_last_checked_line and
    find_stale_active_items (the Alerts screen's Stale Checks section).
    """
    raw = (last_checked_raw or "").strip()
    if not raw:
        return None
    try:
        checked_dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    now = now or datetime.now()
    return max(0, (now - checked_dt).total_seconds() / 3600)


def _format_last_checked_line(last_checked_raw, now=None):
    """
    Return 'Last checked Nh ago'. Falls back to 'unknown' when missing/
    unparseable; appends a STALE warning when older than 12 hours.
    """
    hours_ago = _hours_since_checked(last_checked_raw, now=now)
    if hours_ago is None:
        return "Last checked unknown"
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
    tier = _tier_label(p.get("demand_score"))
    score = _parse_currency(p.get("demand_score"))
    tier_suffix = f" · Score {score:.1f} ({tier})" if (tier and score is not None) else ""

    lines = [
        f"📦 {p.get('title') or '(untitled)'}",
        f"{p.get('category') or '—'} · {p.get('status') or '—'}{tier_suffix}",
        f"Buy ${_format_price(p.get('costco_cost'))}",
    ]
    savings_line = _format_regular_price_line(p.get("regular_price"), p.get("costco_cost"))
    if savings_line:
        lines.append(savings_line)
    lines.append(f"List ${_format_price(p.get('ebay_price'))}")
    lines.append(f"Ship ${_format_price(p.get('ship_cost'))}")

    fee_pct = _format_fee_rate_pct(p.get("fee_rate"))
    fee_line = f"Fees ${_format_price(p.get('ebay_fees'))}"
    if fee_pct:
        fee_line += f" ({fee_pct})"
    lines.append(fee_line)

    ad_budget = _parse_currency(p.get("ad_budget"))
    if ad_budget is not None and ad_budget > 0:
        lines.append(f"Ads ${_format_price(p.get('ad_budget'))} (suggested budget)")

    lines.append(_format_net_fragment(p.get("net_profit"), p.get("net_margin"), label="Net without ads:"))
    net_with_ads_line = _format_net_with_ads_line(p.get("net_profit"), p.get("ad_budget"), p.get("ebay_price"))
    if net_with_ads_line:
        lines.append(net_with_ads_line)

    lines.append(f"Stock: {p.get('stock_status') or '—'}")
    sale_line = _format_sale_line(p.get("sale_info"), now=now)
    if sale_line:
        lines.append(sale_line)
    lines.append(_format_last_checked_line(p.get("last_checked"), now=now))
    lines.append(p.get("costco_url") or "—")
    return "\n".join(lines)


def format_review_card(item, position, total):
    """
    Render a Review-queue swipe card: a position counter over the same body
    a /lookup card uses (format_product_detail) — reused rather than
    re-implementing the ~15 lines of financial-detail formatting a third
    time.
    """
    return f"Item {position} of {total}\n{format_product_detail(item)}"


def format_audit_card(item, position, total):
    """
    Render an Audit-queue swipe card: the same body as format_review_card,
    prefixed with the flag reason from tier_summary (col T) when present —
    where agents/auditor.py writes why a row was sent to AUDIT_REVIEW.
    """
    reason = (item.get("tier_summary") or "").strip()
    reason_line = f"⚠️ Flagged: {reason}\n" if reason else ""
    return f"{reason_line}Item {position} of {total}\n{format_product_detail(item)}"


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
    lines.append("Scores: 0–10 · Tier 1 ≥7 🥇 · Tier 2 ≥4 · Tier 3 <4 · Sharpe = risk-adjusted return (higher = better)")
    lines.append("")
    lines.append(f"Total tracked: {total}")
    lines.append("Last updated: just now")
    return "\n".join(lines)


_DASHBOARD_PRODUCT_FIELDS = (
    "status", "title", "category", "demand_score", "net_profit", "net_margin",
    "comp_saturation", "suggested_price", "sale_info", "ad_budget",
    "costco_cost", "ebay_price", "mpt_sharpe", "mpt_rank",
    "stock_status", "last_checked",
)


def extract_dashboard_products(rows, col_map, data_start_row=_DEFAULT_DATA_START_ROW):
    """
    Build one dict of raw string fields per non-blank-status row, keyed by
    col_map field name, plus row_num (the absolute sheet row). Never raises
    on ragged/short rows.
    """
    status_i = col_to_idx(col_map["status"])

    def has_status(row):
        return bool(safe_get(row, status_i).strip())

    return _extract_rows_by_field(
        rows, col_map, _DASHBOARD_PRODUCT_FIELDS,
        data_start_row=data_start_row, filter_fn=has_status,
    )


def _parse_currency(raw):
    """Parse a '$45.20'-style sheet cell to float, or None if blank/unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _parse_mpt_rank(raw):
    """Parse the leading integer out of an mpt_rank cell ('1 🥇 Best' or '3'). None if blank/unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.match(r'-?\d+', raw)
    return int(m.group(0)) if m else None


def _parse_sharpe(raw):
    """Parse the leading float out of an mpt_sharpe cell ('1.8 🔥 Strong' or '1.8'). None if blank/unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.match(r'-?\d+\.?\d*', raw)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def format_top_opportunities(products, n=3):
    """
    Top n READY products ranked by mpt_rank ascending (1=best); READY rows
    with no parsable rank sort after ranked ones, tiebroken by higher Sharpe.
    Returns None if there are no READY rows.
    """
    ready = [p for p in products if p["status"] == "READY"]
    if not ready:
        return None

    def sort_key(p):
        rank = _parse_mpt_rank(p["mpt_rank"])
        sharpe = _parse_sharpe(p["mpt_sharpe"])
        rank_key = rank if rank is not None else float("inf")
        sharpe_key = -(sharpe if sharpe is not None else float("-inf"))
        return (rank_key, sharpe_key)

    ready.sort(key=sort_key)

    lines = ["🏆 Top Ready to List"]
    for i, p in enumerate(ready[:n], start=1):
        title = p["title"] or "(untitled)"
        buy = _format_price(p["costco_cost"])
        list_price = _format_price(p["ebay_price"])
        net_frag = _format_net_fragment(p["net_profit"], p["net_margin"], label="Net")
        ads = _format_price(p["ad_budget"])
        tier = _tier_label(p["demand_score"])
        tier_flag = " 🥇" if tier == "Tier 1 🥇" else ""
        sat_flag = " ⚠️ saturated" if (p["comp_saturation"] or "").strip().lower() == "high" else ""
        lines.append(
            f"{i}. {title} — Buy ${buy} · List ${list_price} · {net_frag} · Ads ${ads}{tier_flag}{sat_flag}"
        )
    return "\n".join(lines)


def format_sale_urgency_section(products, now=None):
    """
    READY/ACTIVE rows with a parseable sale expiry, soonest first. Sales that
    expired more than 7 days ago are dropped entirely — a stale expired sale
    is noise, not urgency. Returns None if none qualify — the section is
    skipped entirely per spec.
    """
    candidates = []
    for p in products:
        if p["status"] not in ("READY", "ACTIVE"):
            continue
        parsed = _parse_sale_expiry_info(p["sale_info"], now=now)
        if not parsed:
            continue
        exp_str, days_left, raw_days_left = parsed
        if raw_days_left < -7:
            continue
        candidates.append((days_left, p["title"] or "(untitled)", exp_str))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])

    lines = ["🔥 Sale Expiring Soon"]
    for days_left, title, exp_str in candidates:
        lines.append(f"• {title} — sale ends {exp_str} ({days_left}d left)")
    return "\n".join(lines)


def format_category_breakdown(products, category_names=None):
    """
    Per-category READY/ACTIVE counts, in categories.yaml's declared order
    (any category found in the sheet but absent from categories.yaml is
    appended at the end rather than silently dropped). Categories with zero
    of both statuses are omitted. Returns None if nothing qualifies.
    category_names defaults to _load_category_names(); pass explicitly to
    avoid reading the real config (e.g. in tests).
    """
    counts = {}
    for p in products:
        if p["status"] not in ("READY", "ACTIVE"):
            continue
        cat = (p["category"] or "").strip()
        if not cat:
            continue
        counts.setdefault(cat, {"READY": 0, "ACTIVE": 0})
        counts[cat][p["status"]] += 1

    known = category_names if category_names is not None else _load_category_names()
    ordered = known + [c for c in counts if c not in known]

    lines = []
    for cat in ordered:
        c = counts.get(cat)
        if not c or (c["READY"] == 0 and c["ACTIVE"] == 0):
            continue
        parts = []
        if c["READY"]:
            parts.append(f"{c['READY']} Ready")
        if c["ACTIVE"]:
            parts.append(f"{c['ACTIVE']} Active")
        lines.append(f"{cat}: {', '.join(parts)}")

    if not lines:
        return None
    return "\n".join(["📦 Category Breakdown"] + lines)


# Mirrors tools/status_logic.py's own convention for "still has a stock
# issue" — the same three values that block a PAUSED_OOS -> WATCH promotion.
_STOCK_ISSUE_VALUES = {"OUT OF STOCK", "CHECK FAILED", "Limited"}


def find_stale_active_items(products, now=None, stale_hours=12):
    """ACTIVE rows not checked within stale_hours — Alerts screen's Stale Checks section."""
    out = []
    for p in products:
        if (p.get("status") or "").strip() != "ACTIVE":
            continue
        hours = _hours_since_checked(p.get("last_checked"), now=now)
        if hours is not None and hours > stale_hours:
            out.append(p)
    return out


def find_back_in_stock(products):
    """PAUSED_OOS rows whose stock_status no longer reads as an out-of-stock signal."""
    out = []
    for p in products:
        if (p.get("status") or "").strip() != "PAUSED_OOS":
            continue
        stock = (p.get("stock_status") or "").strip()
        if stock and stock not in _STOCK_ISSUE_VALUES:
            out.append(p)
    return out


def _parse_pct(raw):
    """
    Parse a percentage-ish sheet cell (e.g. net_margin, col J) to a plain
    float percentage: '18%' -> 18.0, '0.18' -> 18.0, '18' -> 18.0. None if
    blank/unparseable. Kept separate from _format_fee_rate_pct, which has its
    own tested contract of passing an already-'%'-suffixed cell through
    unchanged as a string rather than reformatting it.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.endswith("%"):
        return _parse_currency(raw[:-1])
    val = _parse_currency(raw)
    if val is None:
        return None
    return val * 100 if 0 < abs(val) < 1 else val


def compute_category_roi(products, category_names=None):
    """
    Average net_margin per category among rows NOT in a PAUSED_*/REJECTED
    status. Unlike format_category_breakdown (which omits zero-count
    categories), every category in category_names always gets a line, even
    "0 items" — Category ROI exists to surface dead categories, not hide
    them. Unknown categories found in the sheet but absent from
    category_names are appended at the end, same convention as
    format_category_breakdown. category_names defaults to
    _load_category_names(); pass explicitly to avoid reading the real config
    (e.g. in tests).
    """
    known = category_names if category_names is not None else _load_category_names()
    by_cat = {c: [] for c in known}
    for p in products:
        status = (p.get("status") or "").strip()
        if status in _PAUSED_STATUSES or status == "REJECTED":
            continue
        cat = (p.get("category") or "").strip()
        by_cat.setdefault(cat, [])
        margin = _parse_pct(p.get("net_margin"))
        if margin is not None:
            by_cat[cat].append(margin)

    lines = ["📈 Category ROI"]
    for cat, vals in by_cat.items():
        if not vals:
            lines.append(f"{cat}: 0 items")
            continue
        avg = sum(vals) / len(vals)
        lines.append(f"{cat}: {avg:.0f}% avg margin ({len(vals)} items)")
    return "\n".join(lines)


# Mirrors tools/spot_price.py's own _KARAT_PURITY table — duplicated rather
# than importing that module's private constant across module boundaries.
_GOLD_KARAT_PURITY = {24: 1.0, 22: 22 / 24, 18: 18 / 24, 14: 14 / 24, 10: 10 / 24}


def compute_spot_price_impact(products, gold_spot, silver_spot):
    """
    For ACTIVE Precious Metals rows, estimate today's melt-based cost from a
    live spot price and the title's parsed weight/karat, and compare against
    the row's stored ebay_price/net_margin to flag how much margin may have
    drifted since the row was last scraped. This is an ESTIMATE, not a live
    recompute — the sheet's own net_profit/net_margin formulas (protected
    columns I/J) don't know about live spot and are left untouched here.

    Pure function: caller supplies gold_spot/silver_spot (e.g. from
    tools.spot_price.get_spot_price) rather than this function fetching them
    itself, so it stays testable without network access or spot_price's
    internal cache/history state.

    Coverage is necessarily partial — jewelry/mixed-metal Precious Metals
    rows routinely have no parseable weight (gemstones, mixed materials) —
    so results explicitly separate "N items estimated" from "M skipped"
    rather than presenting silent partial coverage as if it were complete.
    """
    estimated = []
    skipped = 0
    for p in products:
        if (p.get("status") or "").strip() != "ACTIVE":
            continue
        if (p.get("category") or "").strip() != "Precious Metals":
            continue
        title = p.get("title") or ""
        weight_oz, karat = parse_gold_weight(title)
        if not weight_oz:
            skipped += 1
            continue
        is_silver = "silver" in title.lower()
        spot = silver_spot if is_silver else gold_spot
        ebay_price = _parse_currency(p.get("ebay_price"))
        if spot is None or not ebay_price or ebay_price <= 0:
            skipped += 1
            continue
        purity = _GOLD_KARAT_PURITY.get(karat or 24, 1.0)
        est_cost = spot * weight_oz * purity
        est_margin = (ebay_price - est_cost) / ebay_price * 100
        estimated.append({
            "title": title,
            "stored_margin": _parse_pct(p.get("net_margin")),
            "est_margin": est_margin,
        })

    lines = ["🪙 Spot Price Impact"]
    if gold_spot is not None:
        lines.append(f"Gold: ${gold_spot:,.2f}/oz")
    if silver_spot is not None:
        lines.append(f"Silver: ${silver_spot:,.2f}/oz")
    lines.append(
        f"{len(estimated)} Precious Metals items re-estimated · "
        f"{skipped} skipped (no parseable weight)"
    )
    for item in estimated:
        was = f"{item['stored_margin']:.0f}%" if item["stored_margin"] is not None else "—"
        lines.append(f"  • {item['title']} — margin ~{item['est_margin']:.0f}% (was {was})")
    return "\n".join(lines)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _authorized(update, chat_id):
    cid = update.effective_chat.id if update.effective_chat else None
    if cid != chat_id:
        logger.debug(f"Ignored message from unauthorized chat {cid}")
        return False
    return True


# ── Sheet write guard ────────────────────────────────────────────────────────

# config/col_map.yaml's own header comment documents these as formula columns
# (net_profit, net_margin, comp_saturation, total_cost, ebay_fees, tax_est,
# site_profit, ad_budget) — never overwrite them with an agent/bot write.
PROTECTED_COLS = {"I", "J", "N", "Z", "AC", "AF", "AG", "AH"}


def safe_write_row(service, sheet_name, row_num, col_value_pairs):
    """
    Wraps tools.sheet_writer.write_row_partial with a hard stop against ever
    writing to a formula column. Raises ValueError rather than silently
    dropping the offending pair — a silent drop would look like a successful
    write to the caller while quietly doing nothing, which is worse than a
    loud failure for a money-affecting sheet. Every bot write-back action
    (Approve/Pause/Audit/Keep/Delete) must go through this, never
    write_row_partial directly.
    """
    bad = [col for col, _ in col_value_pairs if col.upper() in PROTECTED_COLS]
    if bad:
        raise ValueError(f"Refusing to write protected formula column(s): {bad}")
    return write_row_partial(service, sheet_name, row_num, col_value_pairs)


# ── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_help(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return
    text = (
        "<b>WAT Reselling Agent — Commands</b>\n\n"
        "/menu — button-driven home screen (Dashboard, Search, Review, Alerts, Operations, Logs)\n"
        "/start — show the persistent button keyboard\n"
        "/status — last run time, pass/fail, cookie age\n"
        "/logs [mode] — recent log lines (modes: active, audit, daily, research, rotation, discovery, refresh-notes, recheck, telegram_bot)\n"
        "/lookup &lt;term&gt; — search Product Tracker by title or category\n"
        "/dashboard — funnel, top Ready opportunities, ad budget, sale urgency, category health\n"
        "/restart — reload bot after a code update\n"
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

    matches = search_products(rows, col_map, term, data_start_row=start)
    text = format_lookup_reply(matches, term)
    if len(text) > _MAX_MSG:
        text = text[:_MAX_MSG - 20] + "\n[truncated]"
    await update.message.reply_text(text)


async def cmd_dashboard(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return

    try:
        col_map = _load_col_map()
        cfg = _load_business_cfg()
        service = get_sheets_service()
        sheet_name = cfg["sheet_name"]
        start, end = cfg["data_start_row"], cfg["data_end_row"]
        rows = read_sheet(service, f"'{sheet_name}'!A{start}:BA{end}")
    except Exception as e:
        logger.warning(f"/dashboard sheet read failed: {e}")
        await update.message.reply_text(
            "Couldn't reach the product sheet right now — try again in a bit."
        )
        return

    counts, total = count_statuses(rows)
    products = extract_dashboard_products(rows, col_map, data_start_row=start)

    blocks = [format_dashboard_reply(counts, total)]
    for section in (
        format_top_opportunities(products),
        format_sale_urgency_section(products),
        format_category_breakdown(products),
    ):
        if section:
            blocks.append(section)

    text = "\n\n".join(blocks)
    if len(text) > _MAX_MSG:
        text = text[:_MAX_MSG - 20] + "\n[truncated]"
    await update.message.reply_text(text)


async def cmd_restart(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return
    await update.message.reply_text("♻️ Restarting bot — back in a few seconds...")
    logger.info("Restart requested via /restart — stopping gracefully before re-exec")
    context.bot_data["_restart_state"]["reexec"] = True
    context.application.stop_running()


# ── Sheet context helpers (shared by the button-driven screens below) ─────────

def _sheet_ctx():
    """col_map, service, sheet_name, data_start_row, data_end_row — no row read."""
    col_map = _load_col_map()
    cfg = _load_business_cfg()
    service = get_sheets_service()
    return col_map, service, cfg["sheet_name"], cfg["data_start_row"], cfg["data_end_row"]


def _read_product_rows():
    """col_map, service, sheet_name, data_start_row, rows — for screens that need actual row data."""
    col_map, service, sheet_name, start, end = _sheet_ctx()
    rows = read_sheet(service, f"'{sheet_name}'!A{start}:BA{end}")
    return col_map, service, sheet_name, start, rows


def _find_by_row_num(items, row_num):
    return next((p for p in items if p["row_num"] == row_num), None)


def _delete_sheet_row(service, sheet_name, row_num, context):
    """
    Delete one row via the Sheets API's deleteDimension (a real row removal,
    not a status write — write_row_partial/safe_write_row don't apply here).
    Caches the sheet's numeric gid on bot_data since resolving it costs an
    extra spreadsheets().get() call.
    """
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    gid = context.bot_data.get("sheet_gid")
    if gid is None:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        gid = next(
            s["properties"]["sheetId"] for s in meta["sheets"]
            if s["properties"]["title"] == sheet_name
        )
        context.bot_data["sheet_gid"] = gid
    body = {"requests": [{"deleteDimension": {"range": {
        "sheetId": gid, "dimension": "ROWS",
        "startIndex": row_num - 1, "endIndex": row_num,
    }}}]}
    service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()


async def _send_screen(update, text, reply_markup=None, parse_mode=None):
    """
    Render a screen whether it was reached via an inline button (edit the
    existing message in place) or via the persistent reply keyboard / a
    command (send a new message) — every menu:* handler below is callable
    from either entry point without duplicating rendering logic.
    """
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


_SHEET_UNREACHABLE_MSG = "Couldn't reach the product sheet right now — try again in a bit."


def _home_inline_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:root")]])


# ── Persistent home keyboard (spec Part 2) ─────────────────────────────────────

_HOME_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Dashboard", "🔍 Search"],
        ["✅ Review", "🚨 Alerts"],
        ["⚙️ Operations", "📋 Logs"],
    ],
    resize_keyboard=True,
)


async def cmd_start(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return
    context.user_data["awaiting_search"] = False
    context.user_data["queue"] = None
    await update.message.reply_text(
        "👋 Welcome to the WAT Reselling Agent. Tap a button below to get started.",
        reply_markup=_HOME_KEYBOARD,
    )


async def cmd_menu(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return
    await cb_menu_root(update, context, None)


# ── Callback data scheme + router ───────────────────────────────────────────
#
# callback_data is always "domain:action" or "domain:action:arg". arg, when
# present, is always a numeric row_num or a short fixed-vocabulary mode/log
# name — never free text, since titles can contain colons/emoji and would
# blow past Telegram's 64-byte callback_data limit.

_CALLBACK_ROUTES = {}  # (domain, action) -> async handler(update, context, arg)


async def on_callback(update, context):
    query = update.callback_query
    if not _authorized(update, context.bot_data["chat_id"]):
        await query.answer()
        return
    await query.answer()  # ack immediately so Telegram clears the tap spinner

    parts = (query.data or "").split(":", 2)
    if len(parts) < 2:
        logger.warning(f"Malformed callback_data: {query.data!r}")
        return
    domain, action = parts[0], parts[1]
    arg = parts[2] if len(parts) > 2 else None

    handler = _CALLBACK_ROUTES.get((domain, action))
    if handler is None:
        logger.warning(f"No route for callback_data: {query.data!r}")
        return
    try:
        await handler(update, context, arg)
    except Exception:
        logger.exception(f"Callback handler failed for {query.data!r}")
        try:
            await query.edit_message_text("Something went wrong — back to menu.", reply_markup=_home_inline_kb())
        except Exception:
            pass


# ── Root menu / Dashboard screens ────────────────────────────────────────────

async def cb_menu_root(update, context, arg):
    context.user_data["awaiting_search"] = False
    context.user_data["queue"] = None
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard", callback_data="menu:dashboard"),
         InlineKeyboardButton("🔍 Search", callback_data="menu:search")],
        [InlineKeyboardButton("✅ Review", callback_data="menu:review"),
         InlineKeyboardButton("🚨 Alerts", callback_data="menu:alerts")],
        [InlineKeyboardButton("⚙️ Operations", callback_data="menu:ops"),
         InlineKeyboardButton("📋 Logs", callback_data="menu:logs")],
    ])
    await _send_screen(update, "🏠 Home — tap a screen:", reply_markup=kb)


async def cb_menu_dashboard(update, context, arg):
    context.user_data["awaiting_search"] = False
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"dashboard screen sheet read failed: {e}")
        await _send_screen(update, _SHEET_UNREACHABLE_MSG, reply_markup=_home_inline_kb())
        return

    counts, total = count_statuses(rows)
    products = extract_dashboard_products(rows, col_map, data_start_row=start)
    blocks = [format_dashboard_reply(counts, total)]
    for section in (
        format_top_opportunities(products),
        format_sale_urgency_section(products),
        format_category_breakdown(products),
    ):
        if section:
            blocks.append(section)
    text = "\n\n".join(blocks)
    if len(text) > _MAX_MSG - 20:
        text = text[:_MAX_MSG - 40] + "\n[truncated]"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="menu:dashboard"),
         InlineKeyboardButton("✅ Review Items", callback_data="menu:review")],
        [InlineKeyboardButton("📤 Export CSV", callback_data="job:export"),
         InlineKeyboardButton("💰 Spot Prices", callback_data="menu:spot")],
        [InlineKeyboardButton("📈 Category ROI", callback_data="menu:roi"),
         InlineKeyboardButton("🏠 Home", callback_data="menu:root")],
    ])
    await _send_screen(update, text, reply_markup=kb)


# ── Search flow (spec Part 4) ────────────────────────────────────────────────
#
# Free text is used in exactly one place in this whole bot: typing a search
# term. Everything else is button taps, so a single "awaiting_search" flag on
# context.user_data plus one MessageHandler does the job of a full
# ConversationHandler with far less state-machine ceremony.

async def cb_menu_search(update, context, arg):
    context.user_data["queue"] = None
    context.user_data["awaiting_search"] = True
    await _send_screen(
        update, "🔎 Type a product name or category to search.", reply_markup=_home_inline_kb()
    )


def _search_action_kb(row_num, status):
    rows_ = []
    if status == "SCORED":
        rows_.append([
            InlineKeyboardButton("✅ Approve", callback_data=f"review:approve:{row_num}"),
            InlineKeyboardButton("⏸️ Pause", callback_data=f"review:pause:{row_num}"),
            InlineKeyboardButton("🔍 Audit", callback_data=f"review:audit:{row_num}"),
        ])
    rows_.append([InlineKeyboardButton("🔍 Search Again", callback_data="menu:search")])
    rows_.append([InlineKeyboardButton("🏠 Home", callback_data="menu:root")])
    return InlineKeyboardMarkup(rows_)


def _search_match_buttons_kb(matches):
    rows_ = [
        [InlineKeyboardButton(f"📦 {(m['title'] or '(untitled)')[:40]} — {m['category'] or '—'}",
                               callback_data=f"search:pick:{m['row_num']}")]
        for m in matches
    ]
    rows_.append([InlineKeyboardButton("🏠 Home", callback_data="menu:root")])
    return InlineKeyboardMarkup(rows_)


async def _handle_search_term(update, context, term):
    if not term:
        await update.message.reply_text("Empty search — try again from the menu.", reply_markup=_home_inline_kb())
        return
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"search sheet read failed: {e}")
        await update.message.reply_text(_SHEET_UNREACHABLE_MSG)
        return

    matches = search_products(rows, col_map, term, data_start_row=start)
    if not matches:
        await update.message.reply_text(f"No products found matching '{term}'.", reply_markup=_home_inline_kb())
    elif len(matches) == 1:
        p = matches[0]
        await update.message.reply_text(
            format_product_detail(p),
            reply_markup=_search_action_kb(p["row_num"], (p.get("status") or "").strip()),
        )
    elif len(matches) <= 5:
        await update.message.reply_text(
            f"Found {len(matches)} matches for '{term}':", reply_markup=_search_match_buttons_kb(matches)
        )
    else:
        await update.message.reply_text("Too many matches — be more specific.", reply_markup=_home_inline_kb())


async def cb_search_pick(update, context, arg):
    row_num = int(arg)
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"search:pick sheet read failed: {e}")
        await _send_screen(update, _SHEET_UNREACHABLE_MSG)
        return
    fields = _LOOKUP_PRODUCT_FIELDS + ("title", "category")
    items = _extract_rows_by_field(rows, col_map, fields, data_start_row=start)
    p = _find_by_row_num(items, row_num)
    if p is None:
        await _send_screen(
            update, "This item is no longer available — it may have been removed.",
            reply_markup=_home_inline_kb(),
        )
        return
    await _send_screen(
        update, format_product_detail(p),
        reply_markup=_search_action_kb(p["row_num"], (p.get("status") or "").strip()),
    )


# ── Review queue (spec Part 5) ───────────────────────────────────────────────

def _active_queue_item(context, row_num):
    """
    The current queue item if row_num matches the queue's current position,
    else None — None means this tap targets a stale/already-handled card (a
    row rendered earlier that the user is tapping again, or one reached
    through Search rather than the swipe queue), so callers fall back to a
    one-off confirmation instead of advancing queue state that doesn't apply.
    """
    q = context.user_data.get("queue")
    if not q or q["pos"] >= len(q["items"]):
        return None
    item = q["items"][q["pos"]]
    if item["row_num"] != row_num:
        return None
    return item


def _review_card_kb(row_num):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"review:approve:{row_num}"),
         InlineKeyboardButton("⏸️ Pause", callback_data=f"review:pause:{row_num}")],
        [InlineKeyboardButton("🔍 Send to Audit", callback_data=f"review:audit:{row_num}"),
         InlineKeyboardButton("⏭️ Skip", callback_data=f"review:skip:{row_num}")],
        [InlineKeyboardButton("🏠 Done", callback_data="menu:root")],
    ])


async def cb_menu_review(update, context, arg):
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"review queue sheet read failed: {e}")
        await _send_screen(update, _SHEET_UNREACHABLE_MSG, reply_markup=_home_inline_kb())
        return
    items = extract_review_queue(rows, col_map, data_start_row=start)
    if not items:
        await _send_screen(
            update, "✅ Review queue is empty — nothing to approve right now.",
            reply_markup=_home_inline_kb(),
        )
        return
    context.user_data["queue"] = {
        "kind": "review", "items": items, "pos": 0,
        "tally": {"approved": 0, "paused": 0, "audited": 0, "skipped": 0},
    }
    await _render_review_position(update, context)


async def _render_review_position(update, context):
    q = context.user_data["queue"]
    item = q["items"][q["pos"]]
    text = format_review_card(item, q["pos"] + 1, len(q["items"]))
    await _send_screen(update, text, reply_markup=_review_card_kb(item["row_num"]))


async def _finish_review_queue(update, context):
    q = context.user_data["queue"]
    t = q["tally"]
    text = (
        f"✅ Review complete — {len(q['items'])} items processed\n"
        f"  Approved: {t['approved']}\n"
        f"  Paused: {t['paused']}\n"
        f"  Sent to Audit: {t['audited']}\n"
        f"  Skipped: {t['skipped']}"
    )
    context.user_data["queue"] = None
    await _send_screen(update, text, reply_markup=_home_inline_kb())


async def _advance_or_finish_review(update, context):
    q = context.user_data["queue"]
    q["pos"] += 1
    if q["pos"] >= len(q["items"]):
        await _finish_review_queue(update, context)
    else:
        await _render_review_position(update, context)


async def cb_review_approve(update, context, arg):
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    col_map, service, sheet_name, start, end = _sheet_ctx()
    safe_write_row(service, sheet_name, row_num, [(col_map["status"], "APPROVED")])
    if item is None:
        await _send_screen(update, "✅ Approved.", reply_markup=_home_inline_kb())
        return
    context.user_data["queue"]["tally"]["approved"] += 1
    await _advance_or_finish_review(update, context)


async def cb_review_skip(update, context, arg):
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    if item is None:
        await _send_screen(update, "Skipped.", reply_markup=_home_inline_kb())
        return
    context.user_data["queue"]["tally"]["skipped"] += 1
    await _advance_or_finish_review(update, context)


async def cb_review_audit(update, context, arg):
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    col_map, service, sheet_name, start, end = _sheet_ctx()
    safe_write_row(service, sheet_name, row_num, [
        (col_map["status"], "AUDIT_REVIEW"),
        (col_map["tier_summary"], "[AUDIT_REVIEW] Sent to audit via Telegram bot"),
    ])
    if item is None:
        await _send_screen(update, "🔍 Sent to audit.", reply_markup=_home_inline_kb())
        return
    context.user_data["queue"]["tally"]["audited"] += 1
    await _advance_or_finish_review(update, context)


async def cb_review_pause(update, context, arg):
    row_num = int(arg)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Out of Stock", callback_data=f"pause:oos:{row_num}"),
         InlineKeyboardButton("📉 Low Margin", callback_data=f"pause:margin:{row_num}")],
        [InlineKeyboardButton("📊 Low Demand", callback_data=f"pause:demand:{row_num}"),
         InlineKeyboardButton("📅 Seasonal", callback_data=f"pause:seasonal:{row_num}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"pause:back:{row_num}")],
    ])
    await _send_screen(update, "Why pause this item?", reply_markup=kb)


_PAUSE_REASON_STATUS = {
    "oos": "PAUSED_OOS", "margin": "PAUSED_MARGIN",
    "demand": "PAUSED_DEMAND", "seasonal": "PAUSED_SEASONAL",
}
# OOS/MARGIN are already re-checked every daily sweep (DAILY_SWEEP_STATUSES in
# tools/status_logic.py); DEMAND/SEASONAL are not, so a bot-driven pause for
# either sets a re_eval_date — without it those rows would never get
# reconsidered, unlike a scheduler-driven pause.
_PAUSE_REASON_REEVAL_DAYS = {"seasonal": 30, "demand": 14}


async def _do_pause(update, context, arg, reason):
    row_num = int(arg)
    status = _PAUSE_REASON_STATUS[reason]
    item = _active_queue_item(context, row_num)
    col_map, service, sheet_name, start, end = _sheet_ctx()
    pairs = [(col_map["status"], status)]
    reeval_days = _PAUSE_REASON_REEVAL_DAYS.get(reason)
    if reeval_days:
        reeval_date = (datetime.now() + timedelta(days=reeval_days)).strftime("%Y-%m-%d")
        pairs.append((col_map["re_eval_date"], reeval_date))
    safe_write_row(service, sheet_name, row_num, pairs)
    if item is None:
        await _send_screen(update, f"⏸️ Paused ({status}).", reply_markup=_home_inline_kb())
        return
    context.user_data["queue"]["tally"]["paused"] += 1
    await _advance_or_finish_review(update, context)


async def cb_pause_oos(update, context, arg):
    await _do_pause(update, context, arg, "oos")


async def cb_pause_margin(update, context, arg):
    await _do_pause(update, context, arg, "margin")


async def cb_pause_demand(update, context, arg):
    await _do_pause(update, context, arg, "demand")


async def cb_pause_seasonal(update, context, arg):
    await _do_pause(update, context, arg, "seasonal")


async def cb_pause_back(update, context, arg):
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    if item is not None:
        await _render_review_position(update, context)
    else:
        await _send_screen(update, "Cancelled.", reply_markup=_home_inline_kb())


# ── Audit queue (spec Part 6) ────────────────────────────────────────────────

def _audit_card_kb(row_num):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Keep", callback_data=f"audit:keep:{row_num}"),
         InlineKeyboardButton("🗑️ Delete", callback_data=f"audit:delete:{row_num}")],
        [InlineKeyboardButton("⏭️ Skip", callback_data=f"audit:skip:{row_num}")],
        [InlineKeyboardButton("🏠 Done", callback_data="menu:root")],
    ])


async def cb_menu_audit(update, context, arg):
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"audit queue sheet read failed: {e}")
        await _send_screen(update, _SHEET_UNREACHABLE_MSG, reply_markup=_home_inline_kb())
        return
    items = extract_audit_queue(rows, col_map, data_start_row=start)
    if not items:
        await _send_screen(
            update, "✅ Audit queue is empty — nothing flagged right now.",
            reply_markup=_home_inline_kb(),
        )
        return
    context.user_data["queue"] = {
        "kind": "audit", "items": items, "pos": 0,
        "tally": {"kept": 0, "deleted": 0, "skipped": 0},
    }
    await _render_audit_position(update, context)


async def _render_audit_position(update, context):
    q = context.user_data["queue"]
    item = q["items"][q["pos"]]
    text = format_audit_card(item, q["pos"] + 1, len(q["items"]))
    await _send_screen(update, text, reply_markup=_audit_card_kb(item["row_num"]))


async def _finish_audit_queue(update, context):
    q = context.user_data["queue"]
    t = q["tally"]
    text = (
        f"✅ Audit complete — {len(q['items'])} items processed\n"
        f"  Kept: {t['kept']}\n"
        f"  Deleted: {t['deleted']}\n"
        f"  Skipped: {t['skipped']}"
    )
    context.user_data["queue"] = None
    await _send_screen(update, text, reply_markup=_home_inline_kb())


async def cb_audit_keep(update, context, arg):
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    col_map, service, sheet_name, start, end = _sheet_ctx()
    safe_write_row(service, sheet_name, row_num, [(col_map["status"], "APPROVED")])
    if item is None:
        await _send_screen(update, "✅ Kept (Approved).", reply_markup=_home_inline_kb())
        return
    q = context.user_data["queue"]
    q["tally"]["kept"] += 1
    q["pos"] += 1
    if q["pos"] >= len(q["items"]):
        await _finish_audit_queue(update, context)
    else:
        await _render_audit_position(update, context)


async def cb_audit_skip(update, context, arg):
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    if item is None:
        await _send_screen(update, "Skipped.", reply_markup=_home_inline_kb())
        return
    q = context.user_data["queue"]
    q["tally"]["skipped"] += 1
    q["pos"] += 1
    if q["pos"] >= len(q["items"]):
        await _finish_audit_queue(update, context)
    else:
        await _render_audit_position(update, context)


async def cb_audit_delete(update, context, arg):
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    title = item["title"] if item else f"row {row_num}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑️ Confirm Delete", callback_data=f"audit_confirm:delete:{row_num}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"audit_confirm:cancel:{row_num}"),
    ]])
    await _send_screen(update, f"Delete '{title}' permanently?", reply_markup=kb)


async def cb_audit_confirm_cancel(update, context, arg):
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    if item is not None:
        await _render_audit_position(update, context)
    else:
        await _send_screen(update, "Cancelled.", reply_markup=_home_inline_kb())


async def cb_audit_confirm_delete(update, context, arg):
    """
    Two-step delete (confirm already happened in cb_audit_delete) + a
    Graveyard-tab write before the row is removed — matching
    agents/auditor.py's own convention for every other automated delete path
    in this codebase, rather than a bare irreversible tap with no record.
    """
    row_num = int(arg)
    item = _active_queue_item(context, row_num)
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"audit delete sheet read failed: {e}")
        await _send_screen(update, _SHEET_UNREACHABLE_MSG)
        return

    fields = _LOOKUP_PRODUCT_FIELDS + ("title", "category")
    current = _find_by_row_num(_extract_rows_by_field(rows, col_map, fields, data_start_row=start), row_num)
    if current is None:
        await _send_screen(
            update, "This item is no longer there — it may have already been handled.",
            reply_markup=_home_inline_kb(),
        )
        return

    write_to_graveyard(service, [{
        "date_removed": datetime.now().strftime("%Y-%m-%d"),
        "reason": "Deleted via Telegram bot (Audit queue)",
        "category": current.get("category", ""),
        "title": current.get("title", ""),
        "cost": current.get("costco_cost", ""),
        "ebay_price": current.get("ebay_price", ""),
        "net_profit": current.get("net_profit", ""),
        "score": current.get("demand_score", ""),
        "status_at_removal": "AUDIT_REVIEW",
        "original_row": row_num,
    }])
    _delete_sheet_row(service, sheet_name, row_num, context)

    if item is not None and context.user_data.get("queue"):
        context.user_data["queue"]["tally"]["deleted"] += 1

    # A deleteDimension shifts every row below the deleted one up by one,
    # invalidating cached row_num for every not-yet-visited queue item.
    # Re-extracting from the sheet is the cheapest correct fix for a queue
    # this small — cheaper than patching cached row numbers in memory.
    try:
        col_map2, service2, sheet_name2, start2, rows2 = _read_product_rows()
        items = extract_audit_queue(rows2, col_map2, data_start_row=start2)
    except Exception as e:
        logger.warning(f"audit re-extract after delete failed: {e}")
        items = []

    if context.user_data.get("queue"):
        context.user_data["queue"]["items"] = items
        context.user_data["queue"]["pos"] = 0

    if not items:
        await _finish_audit_queue(update, context)
    else:
        await _render_audit_position(update, context)


# ── Alerts screen (spec Part 6) ──────────────────────────────────────────────

async def cb_menu_alerts(update, context, arg):
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"alerts screen sheet read failed: {e}")
        await _send_screen(update, _SHEET_UNREACHABLE_MSG, reply_markup=_home_inline_kb())
        return

    products = extract_dashboard_products(rows, col_map, data_start_row=start)
    audit_count = len(extract_audit_queue(rows, col_map, data_start_row=start))
    stale = find_stale_active_items(products)
    back_in_stock = find_back_in_stock(products)
    sale_section = format_sale_urgency_section(products)

    lines = ["🚨 Active Alerts", ""]
    lines.append(sale_section if sale_section else "🔥 Sale Expiring Soon: none")
    lines.append(f"⚠️ Stale Checks: {len(stale)} ACTIVE item(s) not checked in 12h+")
    lines.append(f"📦 Back In Stock: {len(back_in_stock)} item(s)")
    lines.append(f"🗂️ Audit Queue: {audit_count} item(s)")
    text = "\n".join(lines)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗂️ Audit Queue", callback_data="menu:audit")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="menu:alerts"),
         InlineKeyboardButton("🏠 Home", callback_data="menu:root")],
    ])
    await _send_screen(update, text, reply_markup=kb)


# ── Operations / job launching (spec Part 7) ─────────────────────────────────
#
# scheduler.py's own completion notifications (tools/alert_sender.py) only
# fire on crash or a truthy tier-1 result — a routine successful run sends no
# Telegram message today. asyncio.create_subprocess_exec (awaited via a task
# tied to the Application) lets the bot post its own completion message
# instead of relying on that.

_JOB_MODES = ("active", "daily", "research", "discovery", "rotation", "recheck", "audit")
_JOB_LABELS = {
    "active": "▶️ Active Monitor", "daily": "▶️ Daily Sweep", "research": "▶️ Research",
    "discovery": "▶️ Discovery", "rotation": "▶️ Rotation", "recheck": "▶️ Recheck",
    "audit": "▶️ Audit",
}
# refresh-notes intentionally excluded — a one-shot retroactive migration
# per its own docstring, not a routine action; a tap target here risks an
# accidental re-run.


def _running_job(context):
    jobs = context.bot_data.setdefault("jobs", {})
    return next((m for m, p in jobs.items() if p.returncode is None), None)


async def cb_menu_ops(update, context, arg):
    running = _running_job(context)
    text = "⚙️ Operations" + (f"\n⏳ {running} is currently running..." if running else "")

    mode_buttons = [InlineKeyboardButton(_JOB_LABELS[m], callback_data=f"job:start:{m}") for m in _JOB_MODES]
    rows_ = [mode_buttons[i:i + 2] for i in range(0, len(mode_buttons), 2)]
    rows_.append([InlineKeyboardButton("📤 Export CSV", callback_data="job:export")])
    rows_.append([
        InlineKeyboardButton("🔗 Google Sheet",
                              url="https://docs.google.com/spreadsheets/d/1KXxULBBp4dmZb1OMGYPkf_YIE1HFd4byQCsAb-_tSic"),
        InlineKeyboardButton("🔗 eBay Hub", url="https://www.ebay.com/sh/ovw"),
    ])
    rows_.append([InlineKeyboardButton("🔄 Refresh", callback_data="menu:ops"),
                  InlineKeyboardButton("🏠 Home", callback_data="menu:root")])
    await _send_screen(update, text, reply_markup=InlineKeyboardMarkup(rows_))


async def cb_job_start(update, context, arg):
    mode = arg
    running = _running_job(context)
    if running:
        await _send_screen(update, f"⚠️ {running} is currently running — wait for it to finish.")
        return

    cmd = [sys.executable, os.path.join(_BASE_DIR, "agents", "scheduler.py"), "--mode", mode]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=_BASE_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    context.bot_data.setdefault("jobs", {})[mode] = proc
    await _send_screen(
        update, f"▶️ {mode} started — you'll get a message here when it finishes.",
        reply_markup=_home_inline_kb(),
    )
    context.application.create_task(_await_job(context, mode, proc))


async def _await_job(context, mode, proc):
    start_t = time.monotonic()
    try:
        await proc.communicate()
    except Exception:
        logger.exception(f"Error awaiting job {mode}")
    elapsed = time.monotonic() - start_t
    chat_id = context.bot_data["chat_id"]
    if proc.returncode == 0:
        await context.bot.send_message(chat_id, f"✅ {mode} completed ({elapsed:.0f}s)")
    else:
        await context.bot.send_message(chat_id, f"❌ {mode} failed (exit {proc.returncode}) — see /logs {mode}")


async def cb_job_export(update, context, arg):
    await _send_screen(update, "Exporting...")
    try:
        path = await asyncio.to_thread(export_approved_products)
    except Exception as e:
        logger.warning(f"export failed: {e}")
        await context.bot.send_message(context.bot_data["chat_id"], "Export failed — try again in a bit.",
                                        reply_markup=_home_inline_kb())
        return
    if path is None:
        await context.bot.send_message(context.bot_data["chat_id"], "No READY products eligible for export.",
                                        reply_markup=_home_inline_kb())
        return
    with open(path, "rb") as f:
        await context.bot.send_document(context.bot_data["chat_id"], document=f)
    await context.bot.send_message(context.bot_data["chat_id"], "📤 Export complete.", reply_markup=_home_inline_kb())


# ── Category ROI / Spot Prices (spec Parts 8 & 10) ───────────────────────────

async def cb_menu_roi(update, context, arg):
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"roi screen sheet read failed: {e}")
        await _send_screen(update, _SHEET_UNREACHABLE_MSG, reply_markup=_home_inline_kb())
        return
    products = extract_dashboard_products(rows, col_map, data_start_row=start)
    text = compute_category_roi(products)
    await _send_screen(update, text, reply_markup=_home_inline_kb())


async def cb_menu_spot(update, context, arg):
    try:
        col_map, service, sheet_name, start, rows = _read_product_rows()
    except Exception as e:
        logger.warning(f"spot screen sheet read failed: {e}")
        await _send_screen(update, _SHEET_UNREACHABLE_MSG, reply_markup=_home_inline_kb())
        return
    products = extract_dashboard_products(rows, col_map, data_start_row=start)
    gold = await asyncio.to_thread(get_spot_price, "gold")
    silver = await asyncio.to_thread(get_spot_price, "silver")
    text = compute_spot_price_impact(products, gold, silver)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="menu:spot"),
         InlineKeyboardButton("📊 Dashboard", callback_data="menu:dashboard")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu:root")],
    ])
    await _send_screen(update, text, reply_markup=kb)


# ── Logs screen (thin wrapper around the existing /logs body) ───────────────

def _format_log_tail(mode):
    lines = read_tail(LOG_FILES[mode], 30)
    if lines is None:
        return f"{mode}.log: not found"
    safe_lines = [html.escape(l) for l in lines]
    text = f"<pre>{mode}.log (last 30 lines):\n" + "\n".join(safe_lines) + "</pre>"
    if len(text) > _MAX_MSG:
        text = text[:_MAX_MSG - 30] + "\n[truncated]</pre>"
    return text


async def cb_menu_logs(update, context, arg):
    mode_buttons = [InlineKeyboardButton(m, callback_data=f"logs:show:{m}") for m in LOG_FILES]
    rows_ = [mode_buttons[i:i + 2] for i in range(0, len(mode_buttons), 2)]
    rows_.append([InlineKeyboardButton("🏠 Home", callback_data="menu:root")])
    await _send_screen(update, "📋 Pick a log:", reply_markup=InlineKeyboardMarkup(rows_))


async def cb_logs_show(update, context, arg):
    mode = arg
    if mode not in LOG_FILES:
        await _send_screen(update, "Unknown log.", reply_markup=_home_inline_kb())
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Logs", callback_data="menu:logs"),
        InlineKeyboardButton("🏠 Home", callback_data="menu:root"),
    ]])
    await _send_screen(update, _format_log_tail(mode), reply_markup=kb, parse_mode="HTML")


# ── Free-text handler + persistent-keyboard label routing ───────────────────

_HOME_LABEL_HANDLERS = {
    "📊 Dashboard": cb_menu_dashboard,
    "🔍 Search": cb_menu_search,
    "✅ Review": cb_menu_review,
    "🚨 Alerts": cb_menu_alerts,
    "⚙️ Operations": cb_menu_ops,
    "📋 Logs": cb_menu_logs,
}


async def on_text(update, context):
    if not _authorized(update, context.bot_data["chat_id"]):
        return
    text = (update.message.text or "").strip()

    handler = _HOME_LABEL_HANDLERS.get(text)
    if handler is not None:
        context.user_data["awaiting_search"] = False
        await handler(update, context, None)
        return

    if context.user_data.get("awaiting_search"):
        context.user_data["awaiting_search"] = False
        await _handle_search_term(update, context, text)
        return

    await update.message.reply_text(
        "🏠 Use the buttons below, or /help for text commands.", reply_markup=_HOME_KEYBOARD
    )


_CALLBACK_ROUTES.update({
    ("menu", "root"): cb_menu_root,
    ("menu", "dashboard"): cb_menu_dashboard,
    ("menu", "search"): cb_menu_search,
    ("menu", "review"): cb_menu_review,
    ("menu", "audit"): cb_menu_audit,
    ("menu", "alerts"): cb_menu_alerts,
    ("menu", "ops"): cb_menu_ops,
    ("menu", "logs"): cb_menu_logs,
    ("menu", "roi"): cb_menu_roi,
    ("menu", "spot"): cb_menu_spot,
    ("review", "approve"): cb_review_approve,
    ("review", "pause"): cb_review_pause,
    ("review", "audit"): cb_review_audit,
    ("review", "skip"): cb_review_skip,
    ("pause", "oos"): cb_pause_oos,
    ("pause", "margin"): cb_pause_margin,
    ("pause", "demand"): cb_pause_demand,
    ("pause", "seasonal"): cb_pause_seasonal,
    ("pause", "back"): cb_pause_back,
    ("audit", "keep"): cb_audit_keep,
    ("audit", "delete"): cb_audit_delete,
    ("audit", "skip"): cb_audit_skip,
    ("audit_confirm", "delete"): cb_audit_confirm_delete,
    ("audit_confirm", "cancel"): cb_audit_confirm_cancel,
    ("search", "pick"): cb_search_pick,
    ("job", "start"): cb_job_start,
    ("job", "export"): cb_job_export,
    ("logs", "show"): cb_logs_show,
})


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    _check_and_write_pid_lock()
    try:
        _main_body()
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


async def _notify_online(application):
    """post_init callback: tell the authorized chat the bot is ready. Never raises."""
    try:
        await application.bot.send_message(
            application.bot_data["chat_id"], f"✅ Bot back online (PID {os.getpid()})"
        )
    except Exception as e:
        logger.warning(f"Online notice failed to send: {e}")


def _main_body():
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

    app = Application.builder().token(token).post_init(_notify_online).build()
    app.bot_data["chat_id"] = chat_id
    app.bot_data["jobs"] = {}
    app.bot_data["sheet_gid"] = None

    restart_state = {"conflict": False, "reexec": False}
    app.bot_data["_restart_state"] = restart_state

    async def _on_error(update, context):
        error = context.error
        if isinstance(error, Conflict):
            restart_state["conflict"] = True
            logger.warning(
                f"Telegram Conflict ({error}) — another session is still active. Stopping to wait it out."
            )
            context.application.stop_running()
        else:
            logger.error("Unhandled error while processing update", exc_info=error)

    app.add_error_handler(_on_error)

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("lookup", cmd_lookup))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    _start_heartbeat_thread()

    try:
        logger.info("Polling for messages...")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception:
        logger.exception("Bot crashed — exiting so start_telegram_bot.bat can restart the process")
        sys.exit(1)

    if restart_state["reexec"]:
        logger.info("Restarting process via os.execv (graceful /restart)")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    if restart_state["conflict"]:
        logger.warning("Conflict — exiting to let the .bat restart after Telegram releases the session")
        sys.exit(3)
    # clean shutdown (e.g. Ctrl+C) — return normally, exit code 0


if __name__ == "__main__":
    main()
