"""
Tool: sale_monitor
Pure helpers for the sale-aware active monitor (agents/scheduler.py run_active_monitor).
No sheet / browser / network calls — everything here is unit-testable.

The arbitrage model: we list on eBay at a price that assumed Costco's regular cost.
  * Costco SALE START  -> our cost drops, margin improves. Keep the eBay price and pocket it.
  * Costco SALE END    -> our cost jumps back up. Reprice eBay up or the margin is gone.
"""

import json
import os
import re
from datetime import datetime, timedelta

from tools.status_logic import suggest_reprice

SALE_ALERT_STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", ".sale_expiry_alert.json")
_STATE_KEEP_DAYS = 30

PRICE_FLAG_YES = "YES — update listing"


def to_float(value):
    """'$1,299.99' / 39.99 / '' -> float or None (never raises)."""
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_rate(value):
    """Fee rate cell: '13.25%' or 0.1325 or 13.25 -> 0.1325; blank/garbage -> None."""
    f = to_float(str(value).replace("%", ""))
    if f is None:
        return None
    if "%" in str(value) or f >= 1:
        f /= 100
    return f


# ── cost-change classification ───────────────────────────────────────────────

def classify_cost_event(old, new, on_sale, had_sale_badge, threshold):
    """
    Returns "sale_start" | "sale_end" | "drift" | "none".

    old / new: previous col G and freshly scraped price (floats or None).
    on_sale: scraper says the item is discounted now. had_sale_badge: col X was non-blank.

      sale_start  cost dropped by more than `threshold` AND on_sale
      sale_end    cost rose by more than `threshold` (sale ended OR a plain price hike — either
                  way our margin shrank), or a sale badge was showing, the sale is gone and the
                  cost rose at all. A sale that vanished with NO cost rise is not an event (the
                  badge can be a stale DOM false-positive) — the caller just clears col X.
      drift       any other change (small, or a non-sale drop) — write col G silently
      none        nothing to compare (missing old/new) or unchanged
    """
    if new is None or old is None:
        return "none"
    delta = new - old
    if delta < -threshold and on_sale:
        return "sale_start"
    if delta > threshold or (had_sale_badge and not on_sale and delta > 0):
        return "sale_end"
    if abs(delta) > 1e-9:
        return "drift"
    return "none"


def sale_badge(savings, expires):
    """Col X text: '🔥 -$8 ends 10/18/26' (year kept — the expiry check needs M/D/YY)."""
    badge = f"🔥 -${savings:.0f}" if savings else "🔥 SALE"
    if expires:
        badge += f" ends {expires}"
    return badge


def sale_column_updates(COL, costco_data, had_badge, has_regular):
    """
    (col, value) pairs for the sale columns X (badge) / AW (regular price) after a scrape.
    Shared by the active monitor, the daily sweep and sale-refresh so they cannot drift:
      on sale                      -> badge + regular price (AW blank when the API had none)
      not on sale, badge/AW stale  -> clear both
      no price scraped             -> nothing (a scrape miss is not evidence either way)
    """
    if not costco_data.get("price"):
        return []
    if costco_data.get("on_sale"):
        orig = costco_data.get("original_price")
        return [(COL["sale_info"], sale_badge(costco_data.get("sale_savings"),
                                              costco_data.get("sale_expires"))),
                (COL["regular_price"], orig if orig else "")]
    if had_badge or has_regular:
        return [(COL["sale_info"], ""), (COL["regular_price"], "")]
    return []


def badge_verified(sale_info, price, regular, now=None):
    """
    True when a col X badge looks API-written and still live: the regular price (AW) is a real
    number above the sale price (G) and the badge carries a future end date. Anything else is
    "unverified" and worth re-scraping (pre-fix rows hold DOM false-positives like '-$100').
    """
    p, r = to_float(price), to_float(regular)
    if p is None or r is None or not r > p > 0:
        return False
    end = parse_sale_expiry(sale_info, now)
    return end is not None and end >= (now or datetime.now())


def margin_note_sale_start(old, new):
    return f"on sale — margin +${old - new:.2f}"


def sale_end_alert(title, old, new, fee_rate, ship_cost, ebay_price, row=None, category=""):
    """
    Urgent-alert item (send_urgent_alert shape) for a sale end / cost rise.
    fee_rate: fraction (0.1325) or None; ship_cost / ebay_price: floats or None.
    """
    fee = fee_rate if fee_rate is not None else 0.0
    target = suggest_reprice(new, fee, ship_cost or 0) if fee_rate is not None else None
    head = f"sale ended — cost ${old:.2f}→${new:.2f}"
    if target:
        reprice_note = f"Reprice eBay to ${target:.2f} to keep margin"
        reason = f"{head}, reprice eBay to ${target:.2f} to keep margin"
    else:
        reprice_note = "No profitable eBay price at this cost — consider ending the listing"
        reason = f"{head}, no profitable price — consider ending listing"
    if ebay_price and fee_rate is not None:
        net = ebay_price - new - ebay_price * fee - (ship_cost or 0)
        reprice_note += f" (at your ${ebay_price:.2f} net is now ${net:.2f})"
    return {"title": title, "row": row, "category": category,
            "reason": reason, "reprice_note": reprice_note, "target": target}


def price_flag_still_needed(existing_flag, ebay_price, target):
    """
    Col P "YES — update listing" must survive until the listing is fixed (the monitor used to
    blank it on the next quiet run). Returns True while the flag is set and the eBay price is
    still below the reprice target (or either is unknown — never clear on missing data).
    """
    if not str(existing_flag or "").upper().startswith("YES"):
        return False
    if ebay_price is None or target is None:
        return True
    return ebay_price < target - 0.005


# ── sale-expiry countdown ────────────────────────────────────────────────────

_EXPIRY_RX = re.compile(r"ends?\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", re.IGNORECASE)


def parse_sale_expiry(sale_info, now=None):
    """
    Col X text -> end-of-day datetime, or None. Accepts 'ends 10/18/26', 'ends 10/18/2026' and
    year-less 'ends 10/18' (assumes the next such date on/after ~a week ago).
    """
    m = _EXPIRY_RX.search(str(sale_info or ""))
    if not m:
        return None
    now = now or datetime.now()
    month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            y = int(year)
            y += 2000 if y < 100 else 0
            return datetime(y, month, day, 23, 59)
        dt = datetime(now.year, month, day, 23, 59)
        if dt < now - timedelta(days=7):
            dt = datetime(now.year + 1, month, day, 23, 59)
        return dt
    except ValueError:
        return None


def expiry_tier(hours_left, warn_hours, urgent_hours):
    """'urgent' (<= urgent_hours), 'warn' (<= warn_hours), else None. Ended sales -> None."""
    if hours_left <= 0:
        return None
    if hours_left <= urgent_hours:
        return "urgent"
    if hours_left <= warn_hours:
        return "warn"
    return None


def load_alert_state(path=None):
    try:
        with open(path or SALE_ALERT_STATE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def already_alerted(state, key):
    return key in state


def record_alerts(state, keys, path=None, now=None):
    """Mark keys alerted (pruning entries older than 30d) and persist. Never raises."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=_STATE_KEEP_DAYS)).isoformat(timespec="seconds")
    for k in keys:
        state[k] = now.isoformat(timespec="seconds")
    state = {k: v for k, v in state.items() if str(v) >= cutoff}
    try:
        p = path or SALE_ALERT_STATE
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
    except OSError:
        pass
    return state
