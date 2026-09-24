"""
Tool: sale_digest
Pure logic for the "Sale Radar" morning Telegram digest (scheduler --mode sale-digest):
pick the tracked rows that are really on sale, rank them, format one HTML message.
No sheet / network / Telegram calls here.

Col X (sale badge) alone is NOT trusted: rows written before the Sep-2026 price-API fix hold DOM
false-positives ("🔥 -$100" on a $15 vitamin, expired dates, no price). A row only counts when it
has a sale price in col G, a plausible discount, a live (or recently checked) sale. Everything
dropped is counted in `skipped` so bad data stays visible in the Run Log.
"""

import html
import re
from datetime import datetime

from tools.ebay_sync import compute_net
from tools.sale_monitor import parse_rate, parse_sale_expiry, to_float

JUNK_STATUSES = {"", "AUDIT_REVIEW", "PAUSED_DEMAND", "PAUSED_SEASONAL"}
UNDATED_MAX_AGE_DAYS = 2     # a badge with no end date must have been checked this recently
DATED_MAX_AGE_DAYS = 7       # a dated sale that hasn't been re-checked in a week may have ended early
TITLE_MAX = 40
_SAVINGS_RX = re.compile(r"-\s*\$\s*(\d+(?:\.\d+)?)")


def _idx(letter):
    n = 0
    for c in letter.upper():
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


def _cell(row, COL, key):
    i = _idx(COL[key])
    return str(row[i]).strip() if i < len(row) else ""


def _age_days(stamp, now):
    """Days since a 'YYYY-MM-DD H:MM' / date-only stamp; None when blank/unparseable."""
    text = str(stamp or "").strip()
    for fmt, width in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return (now - datetime.strptime(text[:width], fmt)).total_seconds() / 86400
        except ValueError:
            continue
    return None


def select_sale_items(rows, COL, now=None, start_row=4):
    """
    rows: Product Tracker rows (A:AW). Returns (items, skipped) — items ranked net desc
    (unknown last) then soonest end (undated last); skipped = {reason: count}.
    """
    now = now or datetime.now()
    items, skipped = [], {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for n, row in enumerate(rows):
        if not row:
            continue
        badge = _cell(row, COL, "sale_info")
        if not badge:
            continue                                   # not flagged on sale at all
        if _cell(row, COL, "status") in JUNK_STATUSES:
            skip("junk_status")
            continue
        price = to_float(_cell(row, COL, "costco_cost"))
        if not price or price <= 0:
            skip("no_price")
            continue
        end = parse_sale_expiry(badge, now)
        if end is not None and end < now:
            skip("expired")
            continue
        age = _age_days(_cell(row, COL, "last_checked"), now)
        limit = DATED_MAX_AGE_DAYS if end is not None else UNDATED_MAX_AGE_DAYS
        if age is None or age > limit:
            skip("stale")
            continue

        regular = to_float(_cell(row, COL, "regular_price"))
        if regular is not None and regular > 0:
            if regular <= price:
                skip("implausible")
                continue
        else:
            regular = None
            m = _SAVINGS_RX.search(badge)
            if m and float(m.group(1)) >= price:       # "-$100" on a $15 item
                skip("implausible")
                continue

        ebay = to_float(_cell(row, COL, "ebay_price"))
        fee = parse_rate(_cell(row, COL, "fee_rate"))
        ship = to_float(_cell(row, COL, "ship_cost")) or 0.0
        net = None
        if ebay and ebay > 0 and fee is not None:
            net = round(compute_net(ebay, price, fee, ship), 2)
        items.append({"title": _cell(row, COL, "title"), "price": price, "regular": regular,
                      "end": end, "ebay": ebay if ebay and ebay > 0 else None, "net": net,
                      "status": _cell(row, COL, "status"), "row": n + start_row})

    items.sort(key=lambda i: (i["net"] is None, -(i["net"] or 0), i["end"] or datetime.max))
    return items, skipped


def _line(item, now, warn_hours):
    title = item["title"]
    if len(title) > TITLE_MAX:                         # truncate BEFORE escaping (no cut entities)
        title = title[:TITLE_MAX - 1].rstrip() + "…"
    detail = f"${item['price']:.2f}"
    extras = []
    if item["regular"]:
        extras.append(f"was ${item['regular']:.2f}")
    urgent = False
    if item["end"]:
        extras.append(f"ends {item['end'].month}/{item['end'].day}")
        urgent = 0 < (item["end"] - now).total_seconds() / 3600 <= warn_hours
    if extras:
        detail += f" ({', '.join(extras)})"
    ebay = f"eBay ${item['ebay']:.2f}" if item["ebay"] else "eBay —"
    if item["net"] is None:
        net = "net n/a"
    else:
        net = f"net {'+' if item['net'] >= 0 else '-'}${abs(item['net']):.2f}"
    return f"• {'⏰ ' if urgent else ''}{html.escape(title)} — {detail} | {ebay} | {net}"


def format_digest(items, top=10, now=None, warn_hours=48):
    """The Telegram HTML message, or None when nothing is on sale (send nothing)."""
    if not items:
        return None
    now = now or datetime.now()
    n = len(items)
    head = f"🛒 <b>Sale Radar — {n} item{'s' if n != 1 else ''} on sale</b>"
    lines = [_line(i, now, warn_hours) for i in items[:top]]
    if n > top:
        lines.append(f"...and {n - top} more")
    return "\n".join([head, *lines])
