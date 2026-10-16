"""
Sales log — every eBay sale with its real timestamp and net
===========================================================
data/sales_log.json = {item_id: [{transaction_id, created, quantity, price, fee, net}, ...]}

Filled by tools/ebay_sync.sync_actual_sales from GetItemTransactions (real TransactionPrice +
FinalValueFee). `net` = actual_net_per_unit(txn, cost_basis) x quantity, frozen once known
(a later Costco cost change must not rewrite past profit); None when the row had no cost or
fee — filled in by a later run once both are known. With managed payments eBay's Trading API
sends NO FinalValueFee (verified live 2026-10-16), so ebay_sync estimates it from the real
sale price (`fee_estimated: true`); a real fee arriving later replaces the estimate.
Entries are de-duplicated by transaction_id (created+price when eBay sends none).

Refunds: each entry also carries `refunded` (succeeded refunds, $) and `shipped`. A refund on
an order that never shipped, or a refund of the whole price, = `cancelled` — left out of units,
orders and net (e.g. GreenPan 10/15/26: paid $103.99, refunded $88.07 3h later, never shipped).
A partial refund on a shipped order stays a sale, its net reduced by the refund. Refund fields
are refreshed every time eBay returns the transaction (a refund changes its ModTime, so it
shows up in the normal 30-day window).

Months are calendar months in Central time (Jay's), newest first.

The bot's Active Listings screen reads summary(): lifetime "Made" and 15/30/60/90-day net.
Local file (gitignored) — the bot and the ebay_sync task both run on this machine.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from loguru import logger

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("America/Chicago")
except Exception:   # no tzdata on this box -> UTC months (off by a few hours at month ends)
    _LOCAL_TZ = timezone.utc

SALES_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "sales_log.json")
WINDOWS = (15, 30, 60, 90)
RECENT_DAYS = 90      # "Made (90d)" / "Sold (90d)" / By-month horizon


def load(path=None) -> dict:
    """The log, or {} when missing / unreadable (logged — never raises)."""
    path = path or SALES_LOG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning(f"sales_log: {path} unreadable ({e}) — treating as empty")
        return {}


def save(log: dict, path=None) -> None:
    """Atomic write (tmp + replace) so a crash can't leave half a file."""
    path = path or SALES_LOG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _key(e: dict) -> str:
    tid = str(e.get("transaction_id") or "").strip()
    return tid or f"{e.get('created')}|{e.get('price')}"


def has_item(log: dict, item_id) -> bool:
    return bool(log.get(str(item_id)))


def needs_backfill(log: dict, item_id) -> bool:
    """True when the item should get the full 90-day fetch: never logged, or a logged sale
    still has an unknown net (it may be older than the normal 30-day window)."""
    entries = log.get(str(item_id)) or []
    return not entries or any(e.get("net") is None for e in entries)


def _is_cancelled(price, qty, refunded, shipped) -> bool:
    """Refund on an unshipped order, or a refund of (nearly) the whole price."""
    if not refunded or refunded <= 0.005:
        return False
    if shipped is False:
        return True
    return price is not None and refunded >= price * max(int(qty or 1), 1) - 0.01


def add_transactions(log: dict, item_id, txns: list[dict], cost_basis) -> int:
    """Append new transactions for item_id (mutates log). Returns how many were new."""
    from tools.ebay_sync import actual_net_per_unit   # ebay_sync imports this module

    entries = log.setdefault(str(item_id), [])
    by_key = {_key(e): e for e in entries}
    added = 0
    for t in txns:
        qty = max(int(t.get("quantity") or 1), 1)
        per_unit = actual_net_per_unit(t, cost_basis)
        entry = {"transaction_id": str(t.get("transaction_id") or ""),
                 "created": t.get("created") or "", "quantity": qty,
                 "price": t.get("price"), "fee": t.get("fee"),
                 "net": round(per_unit * qty, 2) if per_unit is not None else None}
        if t.get("fee_estimated"):
            entry["fee_estimated"] = True
        if "refunded" in t or "shipped" in t:
            entry["refunded"] = round(float(t.get("refunded") or 0.0), 2)
            entry["shipped"] = t.get("shipped")
            entry["cancelled"] = _is_cancelled(entry["price"], qty, entry["refunded"],
                                               entry["shipped"])
        old = by_key.get(_key(entry))
        if old is not None:
            if "refunded" in entry:     # refund status is live data — always refresh it
                old.update(refunded=entry["refunded"], shipped=entry["shipped"],
                           cancelled=entry["cancelled"])
            # upgrade in place: unknown net -> known, estimated fee -> real fee. A known net
            # is otherwise frozen (cost moves later must not rewrite past profit).
            if entry["net"] is not None and (old.get("net") is None
                                             or (old.get("fee_estimated")
                                                 and not entry.get("fee_estimated"))):
                old.update(fee=entry["fee"], net=entry["net"])
                if entry.get("fee_estimated"):
                    old["fee_estimated"] = True
                else:
                    old.pop("fee_estimated", None)
            continue
        by_key[_key(entry)] = entry
        entries.append(entry)
        added += 1
    if not entries:
        log.pop(str(item_id), None)
    return added


def cancelled_units(log: dict) -> dict:
    """{item_id: units in cancelled/refunded orders} — what ebay_sync subtracts from eBay's raw
    QuantitySold (which still counts them) before writing col U. Items with no cancelled
    sale are absent."""
    out = {}
    for iid, entries in log.items():
        n = sum(int(e.get("quantity") or 1) for e in entries or [] if e.get("cancelled"))
        if n:
            out[str(iid)] = n
    return out


def _parse_created(s):
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summary(log: dict, now=None, windows=WINDOWS) -> dict:
    """
    Over every logged sale except cancelled ones:
      net, units, orders (= sales), unknown, estimated, cancelled — lifetime
      windows   {days: net}                        rolling, by sale time
      d90       {units, orders, net}               last RECENT_DAYS days
      months    [{key "2026-10", label "Oct", units, orders, net}, ...] — calendar months
                (Central time) with a sale in the last RECENT_DAYS days, newest first;
                label gets the year when it isn't the current year ("Dec 2026").
    net sums only sales with a known net (minus partial refunds); `unknown` counts the rest
    (still in units/orders); `estimated` = sales whose eBay fee was estimated. A sale whose
    `created` won't parse counts toward lifetime only.
    """
    now = now or datetime.now(timezone.utc)
    cutoffs = {d: now - timedelta(days=d) for d in windows}
    recent_cut = now - timedelta(days=RECENT_DAYS)
    this_year = now.astimezone(_LOCAL_TZ).year
    out = {"net": 0.0, "units": 0, "sales": 0, "orders": 0, "unknown": 0, "estimated": 0,
           "cancelled": 0, "windows": {d: 0.0 for d in windows},
           "d90": {"units": 0, "orders": 0, "net": 0.0}, "months": []}
    months = {}
    for entries in log.values():
        for e in entries or []:
            if e.get("cancelled"):
                out["cancelled"] += 1
                continue
            qty = int(e.get("quantity") or 1)
            out["sales"] += 1
            out["units"] += qty
            if e.get("fee_estimated"):
                out["estimated"] += 1
            net = e.get("net")
            if net is not None:
                net -= float(e.get("refunded") or 0.0)      # partial refund on a shipped order
            else:
                out["unknown"] += 1
            created = _parse_created(e.get("created"))
            if net is not None:
                out["net"] += net
            if created is None:
                continue
            if net is not None:
                for d, cut in cutoffs.items():
                    if created >= cut:
                        out["windows"][d] += net
            if created < recent_cut:
                continue
            out["d90"]["units"] += qty
            out["d90"]["orders"] += 1
            local = created.astimezone(_LOCAL_TZ)
            key = f"{local.year:04d}-{local.month:02d}"
            m = months.setdefault(key, {"key": key, "label": local.strftime("%b")
                                        + ("" if local.year == this_year else f" {local.year}"),
                                        "units": 0, "orders": 0, "net": 0.0})
            m["units"] += qty
            m["orders"] += 1
            if net is not None:
                out["d90"]["net"] += net
                m["net"] += net
    out["orders"] = out["sales"]
    out["net"] = round(out["net"], 2)
    out["windows"] = {d: round(v, 2) for d, v in out["windows"].items()}
    out["d90"]["net"] = round(out["d90"]["net"], 2)
    out["months"] = [{**m, "net": round(m["net"], 2)}
                     for _, m in sorted(months.items(), reverse=True)]
    return out
