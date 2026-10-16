"""
Sales log — every eBay sale with its real timestamp and net
===========================================================
data/sales_log.json = {item_id: [{transaction_id, created, quantity, price, fee, net}, ...]}

Filled by tools/ebay_sync.sync_actual_sales from GetItemTransactions (real TransactionPrice +
FinalValueFee). `net` = actual_net_per_unit(txn, cost_basis) x quantity, frozen when the sale
is first logged (a later Costco cost change must not rewrite past profit); None when the row
had no cost. Entries are de-duplicated by transaction_id (created+price when eBay sends none).

The bot's Active Listings screen reads summary(): lifetime "Made" and 15/30/60/90-day net.
Local file (gitignored) — the bot and the ebay_sync task both run on this machine.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from loguru import logger

SALES_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "sales_log.json")
WINDOWS = (15, 30, 60, 90)


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


def add_transactions(log: dict, item_id, txns: list[dict], cost_basis) -> int:
    """Append new transactions for item_id (mutates log). Returns how many were new."""
    from tools.ebay_sync import actual_net_per_unit   # ebay_sync imports this module

    entries = log.setdefault(str(item_id), [])
    seen = {_key(e) for e in entries}
    added = 0
    for t in txns:
        qty = max(int(t.get("quantity") or 1), 1)
        per_unit = actual_net_per_unit(t, cost_basis)
        entry = {"transaction_id": str(t.get("transaction_id") or ""),
                 "created": t.get("created") or "", "quantity": qty,
                 "price": t.get("price"), "fee": t.get("fee"),
                 "net": round(per_unit * qty, 2) if per_unit is not None else None}
        if _key(entry) in seen:
            continue
        seen.add(_key(entry))
        entries.append(entry)
        added += 1
    if not entries:
        log.pop(str(item_id), None)
    return added


def _parse_created(s):
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summary(log: dict, now=None, windows=WINDOWS) -> dict:
    """
    {net, units, sales, unknown, windows: {days: net}} over every logged sale.
    net / windows sum only sales with a known net; `unknown` counts the rest (still in
    units/sales). A sale whose `created` won't parse counts toward lifetime only.
    """
    now = now or datetime.now(timezone.utc)
    cutoffs = {d: now - timedelta(days=d) for d in windows}
    out = {"net": 0.0, "units": 0, "sales": 0, "unknown": 0, "windows": {d: 0.0 for d in windows}}
    for entries in log.values():
        for e in entries or []:
            out["sales"] += 1
            out["units"] += int(e.get("quantity") or 1)
            net = e.get("net")
            if net is None:
                out["unknown"] += 1
                continue
            out["net"] += net
            created = _parse_created(e.get("created"))
            if created is None:
                continue
            for d, cut in cutoffs.items():
                if created >= cut:
                    out["windows"][d] += net
    out["net"] = round(out["net"], 2)
    out["windows"] = {d: round(v, 2) for d, v in out["windows"].items()}
    return out
