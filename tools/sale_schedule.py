"""
Scheduled reprice / End listing — sale-end store, pre-stage eligibility, action store.

Flow (see agents/scheduler.py run_apply_scheduled, every 10 min):
  1. run_active_monitor scrapes each ACTIVE row; the Costco price API's exact sale end
     (promotionEndDate -> scrape_costco()["sale_end_ts"], UTC ISO) is kept in
     data/.sale_end_times.json keyed by Costco product id (record_sale_end / clear_sale_end).
  2. PRESTAGE_LEAD_HOURS before that end, an ACTIVE row with an eBay item id in col Q gets a
     Telegram prompt (prestage_candidates): "✓ Reprice at sale end" / "⛔ End at sale end" / Ignore.
  3. A tap stores an approved action in data/.scheduled_actions.json (schedule_action). Nothing
     is changed on eBay then.
  4. At apply_at (= the sale end) apply_scheduled re-scrapes the product LIVE and only revises /
     ends when the sale is really over; a later end = sale extended -> reschedule().

Never auto-applies anything that wasn't approved by a tap. Both the bot and the scheduler write
the action store, so every mutation re-reads the file and writes it atomically.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

from loguru import logger

from tools.reprice import restore_margin_price
from tools.sale_monitor import parse_rate, to_float
from tools.status_logic import suggest_reprice

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SALE_END_PATH = os.path.join(_BASE_DIR, "data", ".sale_end_times.json")
ACTIONS_PATH = os.path.join(_BASE_DIR, "data", ".scheduled_actions.json")
OOS_ENDED_PATH = os.path.join(_BASE_DIR, "data", ".oos_ended.json")   # listings auto-ended on OOS

PRESTAGE_LEAD_HOURS = 24        # prompt this long before the sale ends (less left -> at once)
APPLIED_KEEP_DAYS = 30          # history of executed actions
RECENT_APPLY_DAYS = 3           # a scheduled reprice this recent suppresses the reactive prompt
GIVE_UP_HOURS = 12              # can't verify the sale end this long after apply_at -> drop + alert
ACTIONS = ("reprice", "end")

_PID_RE = re.compile(r"(?:\.product\.|/p/-/[^/?#]+/)(\d{5,})")


# ── small helpers ────────────────────────────────────────────────────────────

def costco_product_id(url):
    """Costco product id from either URL shape ('.product.<id>.html' or '/p/-/slug/<id>')."""
    m = _PID_RE.search(url or "")
    return m.group(1) if m else None


def parse_ts(value):
    """ISO string -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def format_end(end):
    """'Sat 10/18 11:59 PM PT (Sun 1:59 AM CT)' — Costco ends sales at 11:59 PM Pacific."""
    end = parse_ts(end) if not isinstance(end, datetime) else end
    if end is None:
        return "?"
    try:
        from zoneinfo import ZoneInfo
        pt, ct = end.astimezone(ZoneInfo("America/Los_Angeles")), end.astimezone(ZoneInfo("America/Chicago"))
    except Exception:   # no tzdata -> assume daylight time
        pt, ct = end - timedelta(hours=7), end - timedelta(hours=5)

    def fmt(d, day=True):
        h = d.hour % 12 or 12
        return (f"{d:%a} {d.month}/{d.day} " if day else f"{d:%a} ") + f"{h}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"
    return f"{fmt(pt)} PT ({fmt(ct, day=False)} CT)"


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, data):
    """Atomic write: a reader (bot or scheduler) never sees a half-written file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"sale_schedule: could not write {os.path.basename(path)}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


# ── auto-ended-on-OOS store (data/.oos_ended.json) ───────────────────────────
# {item_id: {title, row, ended_at}} — written when the active monitor ends a listing because
# Costco went OOS; popped by the daily sweep on restock so its note can say "relist on eBay".

def record_oos_ended(item_id, *, title="", row=None, path=None, now=None):
    path = path or OOS_ENDED_PATH
    data = _load(path)
    data[str(item_id)] = {"title": title, "row": row,
                          "ended_at": _iso(now or utcnow())}
    _save(path, data)


def pop_oos_ended(item_id, path=None):
    """The record for item_id (and remove it), or None."""
    path = path or OOS_ENDED_PATH
    data = _load(path)
    entry = data.pop(str(item_id), None)
    if entry is not None:
        _save(path, data)
    return entry


# ── sale-end store (data/.sale_end_times.json) ───────────────────────────────

def load_sale_ends(path=None) -> dict:
    return _load(path or SALE_END_PATH)


def get_sale_end(pid, path=None):
    return load_sale_ends(path).get(str(pid)) if pid else None


def record_sale_end(pid, end_ts, *, sale_price=None, regular_price=None, title="", sku="",
                    path=None, now=None) -> bool:
    """Upsert the exact sale end for a Costco product. True when it changed (new / moved end)."""
    end = parse_ts(end_ts)
    if not pid or end is None:
        return False
    path = path or SALE_END_PATH
    data = load_sale_ends(path)
    old = data.get(str(pid)) or {}
    data[str(pid)] = {"end_ts": _iso(end), "sale_price": sale_price, "regular_price": regular_price,
                      "title": title, "sku": sku, "seen": _iso(now or utcnow())}
    _save(path, data)
    return old.get("end_ts") != _iso(end)


def clear_sale_end(pid, path=None) -> bool:
    path = path or SALE_END_PATH
    data = load_sale_ends(path)
    if data.pop(str(pid), None) is None:
        return False
    _save(path, data)
    return True


def update_sale_end_from_scrape(costco_url, costco_data, *, title="", sku="", path=None):
    """
    Keep the store in step with a fresh scrape: on sale with an exact end -> upsert; a scrape
    that got a price and says NOT on sale -> clear; no price -> leave alone (a miss is not news).
    Returns "recorded" | "moved" | "cleared" | None.
    """
    pid = costco_product_id(costco_url)
    if not pid or not costco_data.get("price"):
        return None
    if costco_data.get("on_sale") and costco_data.get("sale_end_ts"):
        had = get_sale_end(pid, path)
        changed = record_sale_end(pid, costco_data["sale_end_ts"], sale_price=costco_data.get("price"),
                                  regular_price=costco_data.get("original_price"), title=title,
                                  sku=sku, path=path)
        return ("moved" if had else "recorded") if changed else None
    if not costco_data.get("on_sale"):
        return "cleared" if clear_sale_end(pid, path) else None
    return None


# ── action store (data/.scheduled_actions.json) ──────────────────────────────

def load_store(path=None) -> dict:
    data = _load(path or ACTIONS_PATH)
    data.setdefault("actions", {})
    data.setdefault("prompted", {})
    data.setdefault("applied", [])
    return data


def _mutate(fn, path=None):
    """Re-read, apply fn(store), write. Returns fn's result."""
    path = path or ACTIONS_PATH
    store = load_store(path)
    out = fn(store)
    _save(path, store)
    return out


def record_prompt(prompt: dict, path=None):
    """Remember a sent pre-stage prompt (the tap needs its apply_at / costs)."""
    _mutate(lambda s: s["prompted"].__setitem__(str(prompt["item_id"]), prompt), path)


def get_prompt(item_id, path=None):
    return load_store(path)["prompted"].get(str(item_id))


def get_action(item_id, path=None):
    return load_store(path)["actions"].get(str(item_id))


def schedule_action(item_id, action, prompt: dict, path=None, now=None) -> dict:
    """Store an approved action from a pre-stage prompt (replaces any pending one for the item)."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    entry = {
        "action": action, "item_id": str(item_id),
        "target_price": prompt.get("target") if action == "reprice" else None,
        "apply_at": prompt["sale_end_ts"],
        "title": prompt.get("title", ""), "sku": prompt.get("sku", ""),
        "costco_pid": prompt.get("costco_pid"), "costco_url": prompt.get("costco_url", ""),
        "old_cost": prompt.get("old_cost"), "new_cost": prompt.get("new_cost"),
        "ebay_price": prompt.get("ebay_price"),
        "approved_at": _iso(now or utcnow()), "last_error": None,
    }
    _mutate(lambda s: s["actions"].__setitem__(str(item_id), entry), path)
    return entry


def cancel_action(item_id, path=None):
    """Remove a pending action. Returns the removed entry or None."""
    return _mutate(lambda s: s["actions"].pop(str(item_id), None), path)


def due_actions(now=None, path=None) -> list:
    now = now or utcnow()
    return [a for a in load_store(path)["actions"].values()
            if (parse_ts(a.get("apply_at")) or now) <= now]


def reschedule(item_id, apply_at, path=None):
    def fn(s):
        a = s["actions"].get(str(item_id))
        if a:
            a["apply_at"] = _iso(parse_ts(apply_at)) if not isinstance(apply_at, datetime) else _iso(apply_at)
        return a
    return _mutate(fn, path)


def set_last_error(item_id, error, path=None) -> bool:
    """Record an action's last error. True when it differs from the previous one (alert-worthy)."""
    def fn(s):
        a = s["actions"].get(str(item_id))
        if not a or a.get("last_error") == error:
            return False
        a["last_error"] = error
        return True
    return _mutate(fn, path)


def mark_applied(item_id, result: dict, path=None, now=None):
    """Move the action to the applied history (pruned to APPLIED_KEEP_DAYS) and drop its prompt."""
    now = now or utcnow()

    def fn(s):
        a = s["actions"].pop(str(item_id), None) or {"item_id": str(item_id)}
        s["prompted"].pop(str(item_id), None)
        cutoff = now - timedelta(days=APPLIED_KEEP_DAYS)
        s["applied"] = [h for h in s["applied"]
                        if (parse_ts(h.get("applied_at")) or now) >= cutoff]
        s["applied"].append({**a, "result": result, "applied_at": _iso(now)})
        return a
    return _mutate(fn, path)


def recently_applied(item_id, action="reprice", days=RECENT_APPLY_DAYS, path=None, now=None) -> bool:
    """A successful scheduled `action` for item_id within `days` (suppresses the reactive prompt)."""
    now = now or utcnow()
    cutoff = now - timedelta(days=days)
    for h in load_store(path)["applied"]:
        if (h.get("item_id") == str(item_id) and h.get("action") == action
                and (h.get("result") or {}).get("ok")
                and (parse_ts(h.get("applied_at")) or cutoff) >= cutoff):
            return True
    return False


def fast_forward(item_id, now=None, path=None) -> bool:
    """Cost already rose (early revert): make a pending action due now. True if one was pending."""
    now = now or utcnow()
    if str(item_id) not in load_store(path)["actions"]:
        return False                       # read-only when nothing is pending (the common case)

    def fn(s):
        a = s["actions"].get(str(item_id))
        if not a:
            return False
        if (parse_ts(a.get("apply_at")) or now) > now:
            a["apply_at"] = _iso(now)
            a["fast_forwarded"] = True
        return True
    return _mutate(fn, path)


# ── pre-stage eligibility (pure) ─────────────────────────────────────────────

def _col_idx(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _cell(row, COL, key):
    if key not in COL:
        return ""
    i = _col_idx(COL[key])
    return (row[i] if i < len(row) else "") or ""


def prestage_candidates(rows, COL, categories, sale_ends, store, now=None, start_row=4,
                        lead_hours=PRESTAGE_LEAD_HOURS):
    """
    Pre-stage prompts due now -> list of prompt dicts (the format_reprice_prompt / schedule
    shape plus sale_end_ts, costco_pid, costco_url). A row qualifies when it is ACTIVE, has a
    valid eBay item id in col Q, its Costco product has a known sale end with
    now < end <= now + lead_hours (first seen with less left -> at once), it hasn't been prompted
    for THIS end, has no pending action, and restore_margin_price gives a raise
    (sale price G -> regular price AW, else the store's regular price) AND the eBay price is
    below suggest_reprice's 20%-margin price at the regular cost (the reactive path's gate).
    """
    from tools.ebay_sync import extract_item_id
    now = now or utcnow()
    horizon = now + timedelta(hours=lead_hours)
    out = []
    for offset, row in enumerate(rows):
        if not row or _cell(row, COL, "status").strip() != "ACTIVE":
            continue
        item_id = extract_item_id(_cell(row, COL, "ebay_listing_url"))
        pid = costco_product_id(_cell(row, COL, "costco_url"))
        if not item_id or not pid:
            continue
        info = sale_ends.get(pid)
        end = parse_ts((info or {}).get("end_ts"))
        if end is None or not (now < end <= horizon):
            continue
        prev = store.get("prompted", {}).get(item_id)
        if prev and prev.get("sale_end_ts") == _iso(end):
            continue
        if item_id in store.get("actions", {}):
            continue
        category = _cell(row, COL, "category")
        cat_cfg = (categories or {}).get(category) or {}
        sale_cost = to_float(_cell(row, COL, "costco_cost"))
        regular = to_float(_cell(row, COL, "regular_price")) or to_float(info.get("regular_price"))
        ebay = to_float(_cell(row, COL, "ebay_price"))
        fee = parse_rate(_cell(row, COL, "fee_rate"))
        if fee is None:
            fee = parse_rate(cat_cfg.get("fee_rate"))
        ship = to_float(_cell(row, COL, "ship_cost")) or 0.0
        ad_rate = parse_rate(cat_cfg.get("ad_rate", 0)) or 0.0
        # Same gate as the reactive path (run_active_monitor): a listing whose eBay price already
        # covers the 20%-margin price at the regular cost needs nothing — no prompt.
        covers = suggest_reprice(regular, fee, ship) if (regular and fee is not None) else None
        if ebay is not None and covers is not None and ebay >= covers:
            continue
        target = restore_margin_price(sale_cost, regular, ebay, fee, ship, ad_rate)
        if target is None:
            continue
        out.append({
            "item_id": item_id, "title": _cell(row, COL, "title"), "row": start_row + offset,
            "category": category, "sku": _cell(row, COL, "sku"),
            "costco_pid": pid, "costco_url": _cell(row, COL, "costco_url"),
            "old_cost": float(sale_cost), "new_cost": float(regular), "ebay_price": float(ebay),
            "fee_rate": float(fee), "ship": float(ship), "ad_rate": float(ad_rate),
            "target": target, "sale_end_ts": _iso(end),
        })
    return out
