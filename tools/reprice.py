"""
One-tap reprice — pricing, Telegram prompt, pending-prompt store, audit logging.

Flow: the active monitor (scheduler.run_active_monitor) sees a Costco cost RISE on an
ACTIVE row with a live eBay item ID in col Q -> restore_margin_price() -> send_reprice_prompt()
posts a message with "✓ Reprice" / "Ignore" buttons. The tap is handled by
agents/telegram_bot.py (cb_reprice_go / cb_reprice_ignore), which calls
tools.ebay_sync.revise_fixed_price(). Nothing here changes a price on its own.

Routing: the prompt is sent with TELEGRAM_BOT_TOKEN — the same token the polling bot uses —
so the button tap is delivered to that bot's CallbackQueryHandler.

Pricing rule: restore the item's PREVIOUS net profit (net before the cost rise), using the
sheet's net formula (compute_net: price - cost - price*fee - ship - ad, where ad = price*ad_rate).
Never on a cost drop (a sale start keeps the eBay price — Jay pockets the margin), never lower.
"""

import html
import json
import math
import os
import urllib.request
from datetime import datetime, timedelta

from loguru import logger

from tools.ebay_sync import compute_net

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING_PATH = os.path.join(_BASE_DIR, "data", ".reprice_pending.json")
EBAY_SYNC_LOG = os.path.join(_BASE_DIR, "data", "logs", "ebay_sync.log")
PENDING_KEEP_DAYS = 30
BIG_JUMP_PCT = 0.50          # warn in the prompt when the raise exceeds this
CALLBACK_MAX_BYTES = 64      # Telegram's callback_data limit


# ── Pricing ──────────────────────────────────────────────────────────────────

def round_up_99(price: float) -> float:
    """Smallest X.99 that is >= price (47.20 -> 47.99, 47.99 -> 47.99, 48.00 -> 48.99)."""
    p = math.ceil(price * 100 - 1e-6) / 100          # up to the cent (never round down)
    return round(math.ceil(p + 0.01 - 1e-9) - 0.01, 2)


def restore_margin_price(old_cost, new_cost, ebay_price, fee_rate, ship=0.0, ad_rate=0.0):
    """
    eBay price that restores the net profit the listing made BEFORE the cost rise.

    prev_net = compute_net(ebay_price, old_cost, fee_rate, ship, ebay_price * ad_rate)
    target   = (max(prev_net, 0) + new_cost + ship) / (1 - fee_rate - ad_rate), rounded UP to .99

    Returns None when: any input is unknown, the cost did NOT rise (sale start / drop / flat —
    never reprice those), fee + ad >= 100%, or the target would not be above the current eBay
    price (never lower). A listing that was already losing money is restored to break-even,
    not to its old loss.
    """
    try:
        old_cost, new_cost, ebay_price = float(old_cost), float(new_cost), float(ebay_price)
        fee_rate = float(fee_rate)
        ship = float(ship or 0.0)
        ad_rate = float(ad_rate or 0.0)
    except (TypeError, ValueError):
        return None
    if ebay_price <= 0 or new_cost <= old_cost:
        return None
    denom = 1 - fee_rate - ad_rate
    if denom <= 0:
        return None
    prev_net = compute_net(ebay_price, old_cost, fee_rate, ship, ebay_price * ad_rate)
    target = round_up_99((max(prev_net, 0.0) + new_cost + ship) / denom)
    if target <= ebay_price + 0.005:
        return None
    return target


def net_at(price, cost, fee_rate, ship=0.0, ad_rate=0.0) -> float:
    return round(compute_net(price, cost, fee_rate, ship, price * (ad_rate or 0.0)), 2)


# ── Prompt ───────────────────────────────────────────────────────────────────

def _cents(price) -> int:
    return int(round(float(price) * 100))


def reprice_keyboard(item_id, target, retry=False) -> dict:
    """Telegram inline_keyboard dict. callback_data: reprice:go:<item_id>:<cents>."""
    go = f"reprice:go:{item_id}:{_cents(target)}"
    ignore = f"reprice:ignore:{item_id}"
    for data in (go, ignore):
        if len(data.encode("utf-8")) > CALLBACK_MAX_BYTES:
            raise ValueError(f"callback_data too long: {data!r}")
    label = f"🔁 Retry ${target:.2f}" if retry else f"✓ Reprice to ${target:.2f}"
    return {"inline_keyboard": [[{"text": label, "callback_data": go},
                                 {"text": "Ignore", "callback_data": ignore}]]}


def schedule_keyboard(item_id, target) -> dict:
    """Pre-stage (before the sale ends) buttons: schedule a reprice / an End, or ignore."""
    rows = [[{"text": f"✓ Reprice to ${target:.2f} at sale end",
              "callback_data": f"reprice:sched:{item_id}:{_cents(target)}"}],
            [{"text": "⛔ End listing at sale end", "callback_data": f"reprice:schedend:{item_id}"},
             {"text": "Ignore", "callback_data": f"reprice:ignore:{item_id}"}]]
    for row in rows:
        for b in row:
            if len(b["callback_data"].encode("utf-8")) > CALLBACK_MAX_BYTES:
                raise ValueError(f"callback_data too long: {b['callback_data']!r}")
    return {"inline_keyboard": rows}


def cancel_keyboard(item_id) -> dict:
    return {"inline_keyboard": [[{"text": "❌ Cancel scheduled action",
                                  "callback_data": f"reprice:cancel:{item_id}"}]]}


def format_reprice_prompt(item: dict) -> str:
    """
    HTML Telegram text. item keys: title, row, item_id, old_cost, new_cost, ebay_price,
    target, fee_rate, ship, ad_rate (optional sku). Free text is truncated, THEN escaped.
    With item["sale_end_ts"] it is a PRE-STAGE prompt: sent before the sale ends, it names the
    end time and says the chosen action applies automatically then (after a live re-check).
    """
    title = html.escape((item.get("title") or "(untitled)")[:80])
    fee, ship, ad = item["fee_rate"], item.get("ship") or 0.0, item.get("ad_rate") or 0.0
    old_c, new_c, cur, tgt = item["old_cost"], item["new_cost"], item["ebay_price"], item["target"]
    prev_net = net_at(cur, old_c, fee, ship, ad)
    net_now = net_at(cur, new_c, fee, ship, ad)
    net_tgt = net_at(tgt, new_c, fee, ship, ad)
    where = f"row {item.get('row')}" if item.get("row") else ""
    if item.get("sku"):
        where = f"#{html.escape(str(item['sku']))} · {where}" if where else f"#{html.escape(str(item['sku']))}"
    sale_end = item.get("sale_end_ts")
    if sale_end:
        from tools.sale_schedule import format_end
        head = [f"⏰ <b>Sale ends {format_end(sale_end)}</b> — Costco cost goes back up"]
    else:
        head = ["💲 <b>Reprice needed</b> — Costco cost went up"]
    lines = head + [
        f"<b>{title}</b>",
        f"{where} · eBay item {html.escape(str(item['item_id']))}".lstrip(" ·"),
        "",
        f"Costco cost: ${old_c:.2f} → ${new_c:.2f}",
        f"eBay now: ${cur:.2f} — net was ${prev_net:.2f}, now ${net_now:.2f}",
        f"Reprice to: <b>${tgt:.2f}</b> — net ${net_tgt:.2f}",
    ]
    if cur > 0 and (tgt - cur) / cur > BIG_JUMP_PCT:
        lines.append(f"⚠️ That is a {(tgt - cur) / cur:.0%} raise — double-check the cost before tapping.")
    lines.append("")
    if sale_end:
        lines.append("Nothing changes now. Pick an action and it applies automatically when the "
                     "sale ends — only after a live Costco re-check confirms it really ended "
                     "(an extended sale is rescheduled).")
    else:
        lines.append("Tap ✓ to update the live eBay listing. Nothing changes until you tap.")
    return "\n".join(lines)


def send_reprice_prompt(item: dict, token=None, chat_id=None, keyboard=None) -> bool:
    """
    Post the prompt + buttons with the SAME bot token the polling bot uses (TELEGRAM_BOT_TOKEN),
    so the tap routes to its callback handler. Never raises. True iff sent.
    """
    token = (token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        logger.warning("reprice prompt not sent — TELEGRAM_BOT_TOKEN/CHAT_ID unset")
        return False
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": format_reprice_prompt(item),
            "parse_mode": "HTML",
            "reply_markup": keyboard or reprice_keyboard(item["item_id"], item["target"]),
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        logger.info(f"reprice prompt sent: item {item['item_id']} -> ${item['target']:.2f}")
        return True
    except Exception as e:
        logger.warning(f"reprice prompt failed (col P flag still set): {e}")
        return False


# ── Pending prompts (lets the bot re-send a lost / ignored prompt) ───────────

def load_pending(path=None) -> dict:
    try:
        with open(path or PENDING_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pending(data, path=None):
    path = path or PENDING_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError as e:
        logger.warning(f"reprice: could not save pending prompts: {e}")


def save_pending(item: dict, path=None, now=None):
    """Store/replace the prompt for item_id; prunes entries older than PENDING_KEEP_DAYS."""
    now = now or datetime.now()
    data = load_pending(path)
    cutoff = now - timedelta(days=PENDING_KEEP_DAYS)
    data = {k: v for k, v in data.items()
            if _parse_ts(v.get("created")) and _parse_ts(v.get("created")) >= cutoff}
    data[str(item["item_id"])] = {**item, "created": now.strftime("%Y-%m-%d %H:%M")}
    _save_pending(data, path)


def pop_pending(item_id, path=None):
    data = load_pending(path)
    if data.pop(str(item_id), None) is not None:
        _save_pending(data, path)


def get_pending(item_id, path=None):
    return load_pending(path).get(str(item_id))


def _parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


# ── Audit log ────────────────────────────────────────────────────────────────

def log_revise(service, result: dict, *, title="", row=None, old_price=None, source="bot",
               action="reprice", log_path=None):
    """
    Record one reprice outcome in data/logs/ebay_sync.log AND the Run Log tab (mode "reprice").
    result: revise_fixed_price()-shaped dict ({ok, item_id, price, error_kind, error_code,
    message}) or any dict with those keys (e.g. a guard refusal). Never raises.
    """
    old = f"${float(old_price):.2f}" if old_price not in (None, "") else "?"
    new = f"${float(result['price']):.2f}" if result.get("price") is not None else "?"
    iid = result.get("item_id")
    what = {"reprice": f"item {iid} {old}→{new}",
            "end": f"END item {iid}",
            "oos_hide": f"HIDE item {iid} (qty 0)",
            "oos_restore": f"RESTORE item {iid} ({result.get('message') or 'qty ?'})",
            }.get(action, f"{action.upper()} item {iid}")
    if result.get("ok"):
        status = "ok"
        notes = f"{what} | {(title or '')[:40]} (row {row})"
        if result.get("message"):
            notes += f" | {result['message']}"
        errors = ""
    else:
        status = "error"
        code = result.get("error_code") or result.get("error_kind") or "error"
        notes = f"{what} | {(title or '')[:40]} (row {row})"
        errors = f"{code}: {result.get('message', '')} | {notes}"
    line = (f"{datetime.now():%Y-%m-%d %H:%M:%S} | {action}[{source}] | {status.upper()} | "
            f"{errors or notes}")
    try:
        path = log_path or EBAY_SYNC_LOG
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning(f"reprice: ebay_sync.log append failed: {e}")
    try:
        from tools.run_logger import log_run_end, log_run_start
        mode = action if source == "bot" else f"{action}-{source}"   # reprice / reprice-scheduled / end-scheduled
        log_run_end(mode, log_run_start(mode),
                    {"status": status, "notes": notes, "errors": errors},
                    service, dedup=False)
    except Exception as e:
        logger.warning(f"reprice: Run Log write failed: {e}")
    (logger.info if status == "ok" else logger.error)(line)
    return line


def log_ignore(item_id, title="", log_path=None):
    """'Ignore' tap -> one ebay_sync.log line only (no Run Log row: nothing was attempted)."""
    line = (f"{datetime.now():%Y-%m-%d %H:%M:%S} | reprice[bot] | IGNORED | "
            f"item {item_id} | {(title or '')[:40]} — col P flag left set")
    try:
        path = log_path or EBAY_SYNC_LOG
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning(f"reprice: ebay_sync.log append failed: {e}")
    logger.info(line)
    return line
