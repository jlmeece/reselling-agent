"""
Costco -> eBay Monitoring Agent
================================
WAT Framework: Agent layer for monitoring and status management.

Twelve run modes (--mode flag):
  active        3x/day  ACTIVE listings — stock/price, reprice alerts, URGENT SMS
  daily         1x/day  APPROVED->READY (copy+stock verify), PAUSED_OOS stock check
  research      1x/day  PENDING rows — full research + scoring (calls researcher.py logic)
  discovery     1x/day  Find new Costco products, add as PENDING
  rotation      1x/week  Score all active products, flag underperformers, send weekly digest
  refresh-notes one-shot  Retroactively reformat Col T summary line
  recheck       one-shot  Retry Costco scrape for CHECK FAILED and empty-price rows
  audit         every 2 days  Graveyard pass — remove junk, flag borderline rows
  ebay_sync     4x/day     Sync eBay active listings -> units_sold (col U); flag margin breaches
  sale-digest   1x/day     "Sale Radar" Telegram digest of items really on sale (read-only, --dry-run prints)
  sale-refresh  on demand  Re-scrape non-ACTIVE rows with unverified sale badges (writes G/X/AW only)
  savings       1x/day     Costco Member-Only Savings page -> update tracked sales, alert, add in-category PENDING rows
  export        1x/day     Branded photos -> eBay picture hosting -> Seller Hub CSV (Telegram only when it changed)

Run locally: python agents/scheduler.py --mode active
Scheduled via Windows Task Scheduler.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import traceback
import urllib.request
import yaml
from datetime import datetime, timedelta
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import (get_sheets_service, read_sheet, write_row_partial, safe_write_row,
                                ensure_grid_columns, required_grid_columns)
from tools.costco_scraper import (scrape_costco, make_browser, price_miss_message,
                                  inventory_miss_message)
from tools.cookie_refresh import refresh_costco_cookies, cookie_expiry_stats, expiry_refresh_needed
from tools.status_logic import (
    determine_status, suggest_reprice, check_scored_staleness,
    ACTIVE_MONITOR_STATUSES, DAILY_SWEEP_STATUSES, SKIP_STATUSES,
)
from tools.listing_copy import generate_listing_copy
from tools.alert_sender import send_urgent_alert, send_routine_alert, send_ready_to_list_alert, send_rotation_digest, send_run_summary, send_sale_expiry_alert
from tools.run_logger import log_run_start, log_run_end
from tools.sale_history import log_sale
from tools.sale_monitor import (
    PRICE_FLAG_YES, already_alerted, classify_cost_event, expiry_tier, load_alert_state,
    margin_note_sale_start, parse_rate, parse_sale_expiry, price_flag_still_needed,
    record_alerts, sale_column_updates, sale_end_alert, to_float, badge_verified,
)
from tools.sale_digest import select_sale_items, format_digest
from tools.ebay_sync import (extract_item_id, fetch_item_price, revise_fixed_price,
                             set_quantity, set_quantity_zero, compute_net)
from tools.reprice import (log_revise, pop_pending, restore_margin_price, save_pending,
                           schedule_keyboard, send_reprice_prompt)
from tools import sale_schedule as sched
from tools.spot_price import check_spot_movement
from agents.auditor import run_audit
from tools import ebay_sync, costco_savings


# ── Config loaders ────────────────────────────────────────────────────────────

def load_config():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "categories.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def load_col_map():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "col_map.yaml")
    with open(path) as f:
        return yaml.safe_load(f)["columns"]


def col_to_idx(col_letter: str) -> int:
    """Convert column letter(s) to 0-based index. 'A'->0, 'Z'->25, 'AA'->26, 'AC'->28."""
    result = 0
    for c in col_letter.upper():
        result = result * 26 + (ord(c) - ord("A") + 1)
    return result - 1


def safe_get(lst, i, default=""):
    return str(lst[i]).strip() if i < len(lst) else default


# ── Mode: ACTIVE monitor (3x/day) ────────────────────────────────────────────

def _set_cell(row, idx, value):
    """Mirror a sheet write into the in-memory row (pads short rows) so later checks in the
    same run see it."""
    while len(row) <= idx:
        row.append("")
    row[idx] = value


def _reprice_prompt(row, COL, categories, status, title, category, sheet_row,
                    old, new_price, ebay_f, fee_f, ship_f):
    """
    One-tap reprice prompt dict for a cost rise, or None. Only for ACTIVE rows with a valid
    eBay item ID in col Q and a computable restore-margin price above the current eBay price.
    Fee: col AB, else the category's fee_rate; ad: the category's ad_rate (col AE = H*ad_rate).
    """
    if status != "ACTIVE":
        return None
    item_id = extract_item_id(safe_get(row, col_to_idx(COL["ebay_listing_url"])))
    if not item_id:
        return None
    # Scheduled path owns this listing: an approved action that is still pending runs on the
    # next apply_scheduled tick (early revert -> due now, still live-verified); one applied in
    # the last few days already raised the price (restore_margin_price on the NEW eBay price
    # would ask to raise it again).
    if sched.fast_forward(item_id):
        logger.info(f"  Scheduled action pending for eBay {item_id} — made due now, no reactive prompt")
        return None
    if sched.recently_applied(item_id):
        logger.info(f"  Scheduled reprice already applied for eBay {item_id} — no reactive prompt")
        return None
    cat_cfg = categories.get(category) or {}
    fee = fee_f if fee_f is not None else parse_rate(cat_cfg.get("fee_rate"))
    ad_rate = parse_rate(cat_cfg.get("ad_rate", 0)) or 0.0
    target = restore_margin_price(old, new_price, ebay_f, fee, ship_f, ad_rate)
    if target is None:
        return None
    return {"item_id": item_id, "title": title, "row": sheet_row, "category": category,
            "sku": safe_get(row, col_to_idx(COL["sku"])) if "sku" in COL else "",
            "old_cost": float(old), "new_cost": float(new_price), "ebay_price": float(ebay_f),
            "fee_rate": float(fee), "ship": float(ship_f or 0.0), "ad_rate": float(ad_rate),
            "target": target}


OOS_PAUSE_NOTE = "OUT OF STOCK — pause eBay listing immediately to avoid unfilled orders"  # status_logic
OOS_HIDDEN_NOTE = "OUT OF STOCK — eBay listing hidden (quantity 0)"
OOS_HANDLED = ("hidden", "ended_by_ebay", "already_off")   # nothing left for Jay to do now


def _auto_hide_oos(service, item_id, costco_data, title, sheet_row):
    """
    Hide the live eBay listing of an ACTIVE row Costco just reported out of stock: quantity 0 via
    ReviseInventoryStatus (business.ebay_sync.auto_end_oos). With Jay's Out of Stock setting on
    and a GTC listing, eBay keeps it (watchers, sales history, search rank) and the daily sweep
    restores the quantity on restock. Returns (outcome, note); note replaces the OOS line in
    col T / the urgent reason. Never raises. Outcomes:
      "hidden"        — qty 0 ok on a GTC listing (Out of Stock control not reported off)
      "ended_by_ebay" — qty 0 ok, but the listing is fixed-duration or OutOfStockControl is off,
                        so eBay ended it — relist on restock
      "already_off"   — GetItem says the listing isn't Active (e.g. Jay ended it) — nothing sent
      "dom_only"      — the OOS came from page text, not the inventory API's Delivery state -> NOT
                        hidden (the DOM read was wrong both ways on 10/04)
      "auth"          — token rejected; the caller stops auto-hiding for the rest of the run
      "failed"        — any other eBay error / exception; manual fallback via the urgent alert
    The three handled outcomes drop the item's pending scheduled action + reprice prompt and
    record it in data/.oos_hidden.json (prev_qty, hidden) for the daily sweep.
    """
    if costco_data.get("stock_source") != "inventory_api":
        return "dom_only", ("OUT OF STOCK (page text only, not confirmed by Costco's inventory API) — "
                            "NOT auto-hidden; verify on Costco, then set the eBay quantity to 0 by hand")

    def handled(outcome, note, prev_qty=None):
        try:
            sched.cancel_action(item_id)    # a scheduled reprice/Hide at sale end would hit a hidden listing
            pop_pending(item_id)            # ...and so would a re-posted "✓ Reprice" prompt
            sched.record_oos_hidden(item_id, title=title, row=sheet_row, prev_qty=prev_qty,
                                    hidden=(outcome == "hidden"))
        except Exception as e:
            logger.warning(f"  Auto-hide OOS: follow-up bookkeeping failed for eBay {item_id}: {e}")
        return outcome, note

    try:
        info = fetch_item_price(item_id)
        if info.get("error_kind") == "auth":
            log_revise(service, info, title=title, row=sheet_row, source="active_oos", action="oos_hide")
            return "auth", (f"OUT OF STOCK — auto-hide FAILED ({info.get('error_code') or 'auth'}: "
                            f"{(info.get('message') or '')[:120]}) — set the eBay quantity to 0 manually")
        if info.get("ok") and info.get("listing_status") and info["listing_status"] != "Active":
            return handled("already_off", f"OUT OF STOCK — eBay listing already {info['listing_status']} "
                                          "(not Active), nothing to hide")
        res = set_quantity_zero(item_id)
        log_revise(service, res, title=title, row=sheet_row, source="active_oos", action="oos_hide")
    except Exception as e:
        logger.error(f"  Auto-hide OOS: unexpected error for eBay {item_id}: {e}")
        return "failed", f"OUT OF STOCK — auto-hide FAILED (unexpected: {e}) — set the eBay quantity to 0 manually"
    if res.get("ok"):
        prev_qty = info.get("quantity_available") if info.get("ok") else None
        duration, oosc = info.get("listing_duration"), info.get("out_of_stock_control")
        if info.get("ok") and (duration and duration != "GTC" or oosc is False):
            why = f"listing is {duration}, not GTC" if duration and duration != "GTC" else                   "Out of Stock control is off on this listing"
            return handled("ended_by_ebay", f"OUT OF STOCK — eBay quantity set to 0, but {why}, so eBay "
                                            "ENDED it — relist when Costco restocks", prev_qty)
        return handled("hidden", OOS_HIDDEN_NOTE, prev_qty)
    code = res.get("error_code") or res.get("error_kind") or "error"
    note = (f"OUT OF STOCK — auto-hide FAILED ({code}: {(res.get('message') or '')[:120]}) — "
            "set the eBay quantity to 0 manually")
    return ("auth" if res.get("error_kind") == "auth" else "failed"), note


def _restore_oos_listing(service, row, COL, categories, costco_data, title, category, sheet_row,
                         item_id, record):
    """
    Daily sweep, PAUSED_OOS row that Costco restocked and whose listing the monitor hid
    (data/.oos_hidden.json). Restores the eBay quantity — but only if selling still makes money.
    Returns (new_status | None, note, telegram_line | None, kind); new_status None = stay
    PAUSED_OOS and retry tomorrow (record kept). kind: "restored" | "needs_you" | "wait".
      - restock seen only in page text (not the inventory API) -> wait (mirrors the hide guard)
      - GetItem fails -> wait (auth error -> also a Telegram line)
      - listing not Active on eBay (eBay/Jay ended it) -> WATCH + "relist, then update col Q"
      - net < 0 at the LIVE eBay price and the new Costco cost, or margin unknown -> WATCH
      - else set_quantity(prev_qty | col W purchase limit | 99) -> ACTIVE
    Never raises.
    """
    t = html.escape(title[:40])
    if costco_data.get("stock_source") != "inventory_api":
        return None, ("Back in stock per page text only — waiting for Costco's inventory API to "
                      "confirm before restoring the eBay quantity"), None, "wait"
    try:
        info = fetch_item_price(item_id)
        if not info.get("ok"):
            code = info.get("error_code") or info.get("error_kind") or "error"
            note = f"Back in stock — eBay check failed ({code}), retrying the quantity restore tomorrow"
            line = (f"🚨 {t} (row {sheet_row}): eBay token rejected — renew EBAY_AUTH_TOKEN; quantity "
                    "not restored" if info.get("error_kind") == "auth" else None)
            return None, note, line, "wait"
        if info.get("listing_status") != "Active":
            return ("WATCH",
                    f"Back in stock — eBay item {item_id} is {info.get('listing_status') or 'not active'} "
                    "(ended) — Relist it on eBay, then update col Q",
                    f"• {t} (row {sheet_row}): eBay listing {item_id} was ended — Relist it, then "
                    "update col Q", "needs_you")

        price = info["price"]
        cost = to_float(costco_data.get("price")) or to_float(safe_get(row, col_to_idx(COL["costco_cost"])))
        cat_cfg = categories.get(category) or {}
        fee = parse_rate(safe_get(row, col_to_idx(COL["fee_rate"])))
        if fee is None:
            fee = parse_rate(cat_cfg.get("fee_rate"))
        ship = to_float(safe_get(row, col_to_idx(COL["ship_cost"]))) or 0.0
        ad = price * (parse_rate(cat_cfg.get("ad_rate", 0)) or 0.0)
        if cost is None or fee is None:
            return ("WATCH", "Back in stock — couldn't check the margin (cost/fee unknown); the eBay "
                             "listing is still hidden — check it, then raise the quantity by hand",
                    f"• {t} (row {sheet_row}): margin unknown — check, then raise the eBay quantity",
                    "needs_you")
        net = compute_net(price, cost, fee, ship, ad)
        if net < 0:
            return ("WATCH",
                    f"Back in stock — but eBay ${price:.2f} vs Costco ${cost:.2f} loses ${-net:.2f}/sale; "
                    "listing left hidden — reprice, then raise the eBay quantity",
                    f"• {t} (row {sheet_row}): would lose ${-net:.2f}/sale at ${price:.2f} (Costco "
                    f"${cost:.2f}) — reprice, then raise the quantity", "needs_you")

        qty = record.get("prev_qty")
        if not isinstance(qty, int) or qty < 1:
            from tools.ebay_export import _quantity_from_limit
            qty = int(_quantity_from_limit(safe_get(row, col_to_idx(COL["purchase_limit"]))))
        res = set_quantity(item_id, qty)
        log_revise(service, res, title=title, row=sheet_row, source="daily", action="oos_restore")
    except Exception as e:
        logger.error(f"  OOS restore: unexpected error for eBay {item_id}: {e}")
        return None, f"Back in stock — quantity restore failed (unexpected: {e}), retrying tomorrow", None, "wait"
    if res.get("ok"):
        return ("ACTIVE", f"Back in stock — eBay quantity restored to {qty} (net ${net:.2f}/sale)",
                f"• {t} (row {sheet_row}): quantity {qty} at ${price:.2f}, net ${net:.2f}/sale",
                "restored")
    code = res.get("error_code") or res.get("error_kind") or "error"
    msg = (res.get("message") or "")[:120]
    if res.get("error_kind") == "auth":
        return (None, f"Back in stock — eBay token rejected ({code}), retrying the quantity restore tomorrow",
                f"🚨 {t} (row {sheet_row}): eBay token rejected — renew EBAY_AUTH_TOKEN; quantity "
                "not restored", "wait")
    return ("WATCH", f"Back in stock — eBay quantity restore FAILED ({code}: {msg}) — raise it by hand",
            f"• {t} (row {sheet_row}): restore failed ({html.escape(code)}) — raise the quantity by hand",
            "needs_you")


def run_active_monitor(config, COL, service, sheet_name, start_row, end_row, only_rows=None):
    """
    Checks all ACTIVE listings every run.
    - Detects stock changes -> PAUSED_OOS; an OOS live listing (col Q) is hidden on eBay
      (qty 0, _auto_hide_oos — business.ebay_sync.auto_end_oos)
    - Detects Costco SALE START (cost dropped + on_sale) -> writes G / AW / X, keeps the eBay
      price (margin just improved), no reprice suggestion
    - Detects SALE END / cost rise -> col P flag + URGENT reprice-up alert; for an ACTIVE row with
      an eBay item ID in col Q also a Telegram "✓ Reprice" prompt (restore_margin_price — the
      price that restores the pre-rise net). The prompt only ASKS; the bot tap applies it.
    - Trivial cost drift -> col G silently
    - Margin erosion -> note in col T only (no auto-pause; ebay_sync alerts on the live price)
    - Auto-promotes READY->ACTIVE when eBay URL is filled
    - Sale-expiry countdown (from col X, fed by the API's promotionEndDate)
    - Sends URGENT email+SMS if any action needed, otherwise stays silent
    only_rows: optional set of sheet row numbers — scrape/alert only those (live testing).
    """
    business = config["business"]
    categories = config["categories"]

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AW{end_row}")
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    urgent_items = []
    reprice_prompts = []
    checked = 0
    auto_end_oos = (business.get("ebay_sync") or {}).get("auto_end_oos", True)
    oos_handled = []        # listings hidden (qty 0) / found already off on eBay this run
    stop_auto_hide = False  # token rejected -> don't hit the same auth error on every OOS row

    # Pre-scan: skip Chrome entirely if there are no ACTIVE/READY-with-URL/APPROVED rows.
    # This prevents a guaranteed crash on CI (GitHub Actions) where no local Chrome exists.
    has_active = any(
        safe_get(r, col_to_idx(COL["status"])) in ACTIVE_MONITOR_STATUSES
        or safe_get(r, col_to_idx(COL["status"])) == "APPROVED"
        or (safe_get(r, col_to_idx(COL["status"])) == "READY"
            and safe_get(r, col_to_idx(COL["ebay_listing_url"])).startswith("http"))
        for r in all_data if r
    )
    if not has_active:
        logger.info("Active monitor: no ACTIVE or APPROVED listings found — skipping browser launch.")
        return

    with make_browser() as page:
        for idx, row in enumerate(all_data):
            sheet_row = idx + start_row
            if not row:
                continue
            if only_rows and sheet_row not in only_rows:
                continue

            status    = safe_get(row, col_to_idx(COL["status"]))
            ebay_url  = safe_get(row, col_to_idx(COL["ebay_listing_url"]))

            # Process ACTIVE rows, READY rows with eBay URL, and APPROVED rows (stock watch)
            is_approved_check = (status == "APPROVED")
            if (status not in ACTIVE_MONITOR_STATUSES
                    and not (status == "READY" and ebay_url.startswith("http"))
                    and not is_approved_check):
                continue

            title       = safe_get(row, col_to_idx(COL["title"]))
            category    = safe_get(row, col_to_idx(COL["category"]))
            costco_url  = safe_get(row, col_to_idx(COL["costco_url"]))
            costco_cost = safe_get(row, col_to_idx(COL["costco_cost"]))
            ebay_price  = safe_get(row, col_to_idx(COL["ebay_price"]))
            fee_rate    = safe_get(row, col_to_idx(COL["fee_rate"]))
            ship_cost   = safe_get(row, col_to_idx(COL["ship_cost"]))
            demand      = safe_get(row, col_to_idx(COL["demand_score"]))

            if not costco_url.startswith("http"):
                continue

            logger.info(f"Active check: {title[:50]}...")
            checked += 1

            # Scrape Costco
            costco_data = scrape_costco(costco_url, page=page)
            new_price    = costco_data["price"]
            stock_status = costco_data["stock_status"]
            image_urls   = " | ".join(costco_data["image_urls"])
            if costco_data.get("error"):
                logger.warning(f"  Scrape error: {costco_data.get('error')}")
            # Exact sale end (time kept) for the scheduled reprice / End — data/.sale_end_times.json
            _end_change = sched.update_sale_end_from_scrape(
                costco_url, costco_data, title=title, sku=safe_get(row, col_to_idx(COL["sku"])))
            if _end_change:
                logger.info(f"  Sale end {_end_change}: {costco_data.get('sale_end_ts')}")

            # APPROVED rows: only check stock — no margin/demand logic needed yet
            if is_approved_check:
                updates = [
                    (COL["stock_status"], stock_status),
                    (COL["last_checked"], run_time),
                ]
                if image_urls:   # backfill images missed during the Sep-2026 redesign outage
                    updates.append((COL["image_urls"], image_urls))
                if new_price:
                    updates.append((COL["costco_cost"], new_price))
                write_row_partial(service, sheet_name, sheet_row, updates)
                if stock_status == "OUT OF STOCK":
                    urgent_items.append({
                        "title":        title,
                        "row":          sheet_row,
                        "category":     category,
                        "reason":       "OOS BEFORE LISTING — item went out of stock while APPROVED",
                        "reprice_note": "",
                    })
                    logger.warning(f"  APPROVED row OOS: {title[:50]}")
                else:
                    logger.info(f"  APPROVED stock OK: {stock_status}")
                time.sleep(2)
                continue

            # ── Cost change — sale-aware ──────────────────────────────────────────
            # SALE START (cost dropped + on_sale): keep the eBay price, pocket the margin.
            # SALE END / cost rise: flag col P + urgent reprice-up alert.
            # Small drift or a scrape with no price: col G only (or nothing), no alert.
            old        = to_float(costco_cost)
            on_sale    = bool(costco_data.get("on_sale"))
            had_badge  = bool(safe_get(row, col_to_idx(COL["sale_info"])))
            event      = "none"
            if new_price:
                event = classify_cost_event(old, new_price, on_sale, had_badge,
                                            business["price_change_threshold"])
                if event != "none":
                    logger.info(f"  Cost {event}: ${old} -> ${new_price} (on_sale={on_sale})")

            ebay_f = to_float(ebay_price)
            fee_f  = parse_rate(fee_rate)
            ship_f = to_float(ship_cost) or 0.0

            # Compute margin inline (avoid formula column)
            margin = None
            cost_now = to_float(new_price or costco_cost)
            if ebay_f and ebay_f > 0 and cost_now is not None and fee_f is not None:
                margin = (ebay_f - cost_now - ebay_f * fee_f - ship_f) / ebay_f

            try:
                demand_int = int(demand) if demand else None
            except (ValueError, TypeError):
                demand_int = None

            # price_changed is retired as a generic trigger — the sale events below own it.
            new_status, reason_code, notes = determine_status(
                status, stock_status, margin, False, demand_int,
                ebay_url=ebay_url,
                min_margin=business["min_margin_threshold"],
                min_demand=business["min_demand_score"],
            )

            # Append price delta note if Jordan has manually lowered price >5% below recommendation
            suggested_price_raw = safe_get(row, col_to_idx(COL["suggested_price"]))
            try:
                current_p   = float(str(ebay_price).replace("$", "").replace(",", ""))
                suggested_p = float(str(suggested_price_raw).replace("$", "").replace(",", ""))
                if suggested_p > 0 and (suggested_p - current_p) / suggested_p > 0.05:
                    delta_pct = (suggested_p - current_p) / suggested_p
                    delta_note = f"Price set {delta_pct:.0%} below recommendation (${suggested_p:.2f})"
                    notes = f"{notes} | {delta_note}" if notes else delta_note
            except (ValueError, TypeError):
                pass

            # ── OOS on a live listing: hide it on eBay (qty 0). Status stays PAUSED_OOS so the
            #    daily sweep keeps watching and restores the quantity on restock ───────────
            oos_done = False
            if reason_code == "oos" and auto_end_oos and not stop_auto_hide:
                oos_iid = extract_item_id(ebay_url)
                if oos_iid:
                    outcome, oos_note = _auto_hide_oos(service, oos_iid, costco_data, title, sheet_row)
                    notes = (notes.replace(OOS_PAUSE_NOTE, oos_note) if OOS_PAUSE_NOTE in notes
                             else f"{oos_note} | {notes}" if notes else oos_note)
                    if outcome in OOS_HANDLED:
                        oos_done = True
                        oos_handled.append({"title": title, "item_id": oos_iid, "row": sheet_row,
                                            "outcome": outcome,
                                            "removed": bool(costco_data.get("removed"))})
                        logger.info(f"  OOS listing {outcome}: {title[:50]} (eBay {oos_iid})")
                    elif outcome == "auth":
                        stop_auto_hide = True
                        _notify("🚨 <b>eBay token rejected</b> while hiding an OOS listing — renew "
                                "EBAY_AUTH_TOKEN in .env. Remaining OOS listings this run were NOT "
                                "hidden; set their eBay quantity to 0 by hand.")

            # Sale columns X (badge) / AW (regular price): refreshed while on sale, cleared when
            # the sale is gone. A scrape with no price never touches them.
            sale_updates = sale_column_updates(
                COL, costco_data, had_badge, bool(safe_get(row, col_to_idx(COL["regular_price"]))))
            sale_note = None
            sale_item = None
            flag_update = None      # new col P value, when it changes

            existing_flag = safe_get(row, col_to_idx(COL["price_change"]))
            target = suggest_reprice(new_price or old, fee_f, ship_f) if (fee_f is not None and (new_price or old)) else None
            if event == "sale_start":
                sale_note = margin_note_sale_start(old, new_price)
            elif event == "sale_end":
                if ebay_f is not None and target is not None and ebay_f >= target:
                    # Listing price already covers margin at the new cost — nothing to fix
                    sale_note = (f"sale ended — cost ${old:.2f}→${new_price:.2f}, "
                                 f"eBay ${ebay_f:.2f} already covers margin")
                else:
                    flag_update = PRICE_FLAG_YES
                    prompt = _reprice_prompt(row, COL, categories, status, title, category,
                                             sheet_row, old, new_price, ebay_f, fee_f, ship_f)
                    if prompt:
                        reprice_prompts.append(prompt)
                    sale_item = sale_end_alert(title, old, new_price, fee_f, ship_f, ebay_f,
                                               row=sheet_row, category=category,
                                               target=prompt["target"] if prompt else None)
            elif existing_flag and not price_flag_still_needed(existing_flag, ebay_f, target):
                flag_update = ""    # listing was repriced — clear the stale flag
            # (an existing YES flag that is still needed is left alone — it used to be blanked
            #  on the very next quiet run)

            # A re-scrape that reveals a losing ACTIVE listing is always urgent, even when
            # it wasn't a clean "sale ended" event (stale sale badge, small cost drift).
            # Without this, a negative margin only wrote a col T note and could sit
            # unnoticed for days — the exact gap that let a listing go negative for a week.
            if (status == "ACTIVE" and margin is not None and margin < 0
                    and flag_update != PRICE_FLAG_YES and sale_item is None and new_price):
                flag_update = PRICE_FLAG_YES
                prompt = _reprice_prompt(row, COL, categories, status, title, category,
                                         sheet_row, old, new_price, ebay_f, fee_f, ship_f)
                if prompt:
                    reprice_prompts.append(prompt)
                sale_item = sale_end_alert(title, old, new_price, fee_f, ship_f, ebay_f,
                                           row=sheet_row, category=category,
                                           target=prompt["target"] if prompt else None,
                                           kind="losing")
                notes = (f"losing money (margin {margin:.1%})"
                         if notes in ("", "All clear") else f"{notes} | losing money (margin {margin:.1%})")

            if sale_note:
                notes = sale_note if notes in ("", "All clear") else f"{notes} | {sale_note}"

            # Build updates
            updates = [
                (COL["stock_status"],  stock_status),
                (COL["last_checked"],  run_time),
                (COL["tier_summary"],         notes),
                (COL["image_urls"],    image_urls),
            ]
            updates += sale_updates
            if flag_update is not None:
                updates.append((COL["price_change"], flag_update))
            if new_price:
                updates.append((COL["costco_cost"], new_price))
            if new_status != status:
                updates.append((COL["status"], new_status))
                logger.info(f"  Status: {status} -> {new_status}")

            write_row_partial(service, sheet_name, sheet_row, updates)
            for _col, _val in updates:                 # keep the expiry check below in sync
                _set_cell(row, col_to_idx(_col), _val)

            if on_sale and new_price and (event == "sale_start" or not had_badge):
                orig = costco_data.get("original_price")
                log_sale(service, title, category, new_price,
                         f"{orig:.2f}" if orig else "", sale_updates[0][1],
                         coupon_type=costco_data.get("coupon_type") or "",
                         coupon_label=costco_data.get("coupon_label") or "")

            # Collect items needing action (a handled OOS listing needs none — summary below)
            if oos_done:
                if sale_item:
                    urgent_items.append(sale_item)
            elif reason_code not in ("ok", "ebay_url_detected"):
                if sale_item:
                    sale_item["reason"] = f"{sale_item['reason']} | {notes}"
                    urgent_items.append(sale_item)
                else:
                    urgent_items.append({
                        "title":        title,
                        "row":          sheet_row,
                        "category":     category,
                        "reason":       notes,
                        "reprice_note": "",
                    })
            elif sale_item:
                urgent_items.append(sale_item)

            time.sleep(2)

    logger.info(f"Active monitor complete. Checked: {checked} | Urgent: {len(urgent_items)}")

    if oos_handled:
        tag = {"hidden": "", "ended_by_ebay": " ⚠️ eBay ENDED it (not GTC / Out of Stock off) — relist on restock",
               "already_off": " (already off eBay — nothing sent)"}
        removed = [a for a in oos_handled if a.get("removed")]
        oos_only = [a for a in oos_handled if not a.get("removed")]
        lines = []
        if removed:
            lines.append(f"🚫 <b>Removed from Costco — {len(removed)} listing(s) hidden (quantity 0)</b>")
            lines += [f"• {html.escape(a['title'][:40])} (row {a['row']}, eBay {a['item_id']}) — END THIS LISTING" for a in removed]
        if oos_only:
            n_hidden = sum(a["outcome"] == "hidden" for a in oos_only)
            lines.append(f"🙈 <b>Costco out of stock — hid {n_hidden} eBay listing(s) (quantity 0)</b>"
                         if n_hidden else "🙈 <b>Costco out of stock on live listing(s)</b>")
            lines += [f"• {html.escape(a['title'][:40])} (row {a['row']}, eBay {a['item_id']}){tag[a['outcome']]}"
                      for a in oos_only]
        if removed:
            lines.append("Removed items will NOT restock — end them on eBay permanently.")
        if oos_only:
            lines.append("OOS rows are PAUSED_OOS; the daily sweep restores quantity when Costco restocks (if still profitable).")
        _notify("\n".join(lines))
        logger.info(f"OOS handled on eBay: {len(oos_handled)} listing(s), "
                    f"{sum(a['outcome'] == 'hidden' for a in oos_handled)} hidden, {len(removed)} removed")

    # ── One-tap reprice prompts (sale end / cost rise on a live listing) ──────
    # Saved first so the bot can re-send a prompt that failed to send or was ignored.
    for prompt in reprice_prompts:
        save_pending(prompt)
        send_reprice_prompt(prompt)

    # ── Sale expiry check ──────────────────────────────────────────────────────
    # Online arbitrage model — no inventory held. Sale expiry = repricing event.
    # Fed by col X ("🔥 -$8 ends 10/18/26"), which the monitor writes from the price API's
    # promotionEndDate — all_data rows were updated in place above, so a sale seen this run
    # counts this run. Two tiers (SALE_WARN_HOURS / SALE_URGENT_HOURS), each alerted once.
    SALE_WARN_HOURS   = int(business.get("sale_warn_hours",   48))
    SALE_URGENT_HOURS = int(business.get("sale_urgent_hours", 24))
    SALE_EXPIRY_STATUSES = {"ACTIVE", "READY", "APPROVED", "LISTED"}

    alert_state = load_alert_state()
    expiring = []
    expiring_keys = []
    for idx, row in enumerate(all_data):
        if not row:
            continue
        if only_rows and (idx + start_row) not in only_rows:
            continue
        status    = safe_get(row, col_to_idx(COL["status"]))
        sale_info = safe_get(row, col_to_idx(COL["sale_info"]))
        if status not in SALE_EXPIRY_STATUSES or not sale_info:
            continue

        try:
            exp_dt = parse_sale_expiry(sale_info)
            if exp_dt is None:
                logger.debug(f"  Sale badge without a parseable end date: {sale_info!r}")
                continue

            hours_left = (exp_dt - datetime.now()).total_seconds() / 3600
            tier = expiry_tier(hours_left, SALE_WARN_HOURS, SALE_URGENT_HOURS)
            if tier is None:
                continue
            title = safe_get(row, col_to_idx(COL["title"]))
            key = f"{title}|{exp_dt:%Y-%m-%d}|{tier}"
            if already_alerted(alert_state, key):
                continue

            savings_m = re.search(r'\$(\d+\.?\d*)', sale_info)
            savings = to_float(savings_m.group(1)) if savings_m else None
            costco_cost_raw = safe_get(row, col_to_idx(COL["costco_cost"]))
            regular_cost = to_float(safe_get(row, col_to_idx(COL["regular_price"])))
            if regular_cost is None and savings:
                cost_now = to_float(costco_cost_raw)
                regular_cost = (cost_now + savings) if cost_now else None

            expiring.append({
                "title":               title,
                "status":              status,
                "sale_expires":        f"{exp_dt.month}/{exp_dt.day}/{exp_dt:%y}",
                "sale_savings":        savings,
                "costco_url":          safe_get(row, col_to_idx(COL["costco_url"])),
                "ebay_url":            safe_get(row, col_to_idx(COL["ebay_listing_url"])),
                "current_ebay_price":  safe_get(row, col_to_idx(COL["ebay_price"])),
                "costco_cost":         costco_cost_raw,
                "regular_costco_cost": regular_cost,
                "fee_rate":            safe_get(row, col_to_idx(COL["fee_rate"])),
                "ship_cost":           safe_get(row, col_to_idx(COL["ship_cost"])),
                "net_profit":          safe_get(row, col_to_idx(COL["net_profit"])),
                "hours_left":          hours_left,
            })
            expiring_keys.append(key)
        except Exception as e:
            logger.debug(f"  Sale expiry parse error: {e}")

    if expiring:
        min_hours = min(p["hours_left"] for p in expiring)
        send_sale_expiry_alert(expiring, hours_remaining=min_hours)
        record_alerts(alert_state, expiring_keys)
        logger.info(f"  Sale expiry alert — {len(expiring)} listing(s) expiring within {min_hours:.0f}h")

    # Only alert if something actually needs action
    if urgent_items:
        send_urgent_alert(
            subject=f"{len(urgent_items)} listing(s) need immediate action",
            items=urgent_items,
            run_time=run_time,
        )
    else:
        logger.info("No urgent items — no alert sent.")


# ── Mode: APPLY_SCHEDULED (every 10 min) ──────────────────────────────────────

def _notify(text):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return _send_telegram(token, chat_id, text)
    logger.warning("Telegram not configured — notice not sent")
    return False


def _rows_by_item_id(rows, COL, item_id, start_row):
    """[(sheet_row, row)] whose col Q resolves to item_id (row-shift safe identity)."""
    q = col_to_idx(COL["ebay_listing_url"])
    return [(start_row + i, r) for i, r in enumerate(rows)
            if r and extract_item_id(safe_get(r, q)) == item_id]


def _sched_log_line(text):
    """One plain line in data/logs/ebay_sync.log for non-eBay outcomes (extended / skipped)."""
    from tools.reprice import EBAY_SYNC_LOG
    try:
        os.makedirs(os.path.dirname(EBAY_SYNC_LOG), exist_ok=True)
        with open(EBAY_SYNC_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | scheduled | {text}\n")
    except OSError as e:
        logger.warning(f"apply_scheduled: ebay_sync.log append failed: {e}")
    logger.info(f"apply_scheduled: {text}")


def _apply_one(a, page, service, COL, sheet_name, start_row, end_row, threshold, now):
    """
    Execute ONE due, Jay-approved action after a live Costco re-check. Returns a short note.
    Outcomes: applied / already done / rescheduled (sale extended) / retry later (sale not off yet,
    scrape miss, transient eBay error) / dropped (row gone, cost didn't rise, gave up after
    GIVE_UP_HOURS). Every eBay call is logged via log_revise; auth errors alert loudly.
    """
    item_id, action = a["item_id"], a["action"]
    log_action = "sale_end_hide" if action == "end" else action   # Run Log name
    title = a.get("title") or ""
    t = html.escape(title[:70])
    overdue_h = (now - (sched.parse_ts(a.get("apply_at")) or now)).total_seconds() / 3600

    def retry_or_give_up(why):
        if overdue_h >= sched.GIVE_UP_HOURS:
            sched.cancel_action(item_id)
            _sched_log_line(f"GAVE UP {action} item {item_id} after {overdue_h:.0f}h — {why}")
            _notify(f"⚠️ <b>Scheduled {action} dropped</b> — {t}\n{html.escape(why)} for "
                    f"{overdue_h:.0f}h after the sale end. Nothing was changed on eBay; check it by hand.")
            return f"{item_id}: gave up ({why})"
        if sched.set_last_error(item_id, why):
            _sched_log_line(f"RETRY {action} item {item_id} — {why}")
        return f"{item_id}: retry ({why})"

    rows = read_sheet(service, f"'{sheet_name}'!A{start_row}:BB{end_row}")
    matches = _rows_by_item_id(rows, COL, item_id, start_row)
    status_i = col_to_idx(COL["status"])
    if len(matches) != 1 or safe_get(matches[0][1], status_i) != "ACTIVE":
        why = ("not in col Q of any row" if not matches else
               f"in col Q of {len(matches)} rows" if len(matches) > 1 else
               f"status is {safe_get(matches[0][1], status_i) or 'blank'}, not ACTIVE")
        sched.cancel_action(item_id)
        log_revise(service, {"ok": False, "item_id": item_id, "price": a.get("target_price"),
                             "error_kind": "refused", "message": why},
                   title=title, source="scheduled", action=log_action)
        _notify(f"⚠️ Scheduled {action} dropped — {t}: eBay item {item_id} {html.escape(why)}. "
                "Nothing was changed.")
        return f"{item_id}: dropped ({why})"
    row_num, row = matches[0]
    costco_url = safe_get(row, col_to_idx(COL["costco_url"])) or a.get("costco_url")

    # ── live re-check: is the sale really over? ──────────────────────────────
    data = scrape_costco(costco_url, page=page)
    price = data.get("price")
    if not price:
        return retry_or_give_up(f"Costco re-check got no price ({data.get('error') or 'no API price'})")
    new_end = sched.parse_ts(data.get("sale_end_ts"))
    if data.get("on_sale"):
        if new_end and new_end > now:
            sched.reschedule(item_id, new_end)
            sched.update_sale_end_from_scrape(costco_url, data, title=title, sku=a.get("sku", ""))
            _sched_log_line(f"sale extended — {action} item {item_id} rescheduled to {new_end.isoformat()}")
            _notify(f"⏰ <b>Sale extended</b> — {t}\nCostco still ${price:.2f}; your scheduled "
                    f"{action} moved to {sched.format_end(new_end)}. Nothing changed on eBay.")
            return f"{item_id}: rescheduled (sale extended)"
        return retry_or_give_up(f"Costco still shows the sale price ${price:.2f}")
    sched.update_sale_end_from_scrape(costco_url, data)          # sale over -> clear its end
    new_cost = a.get("new_cost")
    if new_cost and price < float(new_cost) - threshold:
        sched.cancel_action(item_id)
        _sched_log_line(f"skipped {action} item {item_id} — Costco cost ${price:.2f}, expected "
                        f"${float(new_cost):.2f}")
        _notify(f"ℹ️ Scheduled {action} skipped — {t}\nSale ended but Costco is ${price:.2f}, not the "
                f"expected ${float(new_cost):.2f}. Nothing changed; the monitor will prompt if the "
                "cost rose.")
        return f"{item_id}: skipped (cost ${price:.2f} < expected)"

    cost_updates = [(COL["costco_cost"], price)]
    cost_updates += sale_column_updates(COL, data, bool(safe_get(row, col_to_idx(COL["sale_info"]))),
                                        bool(safe_get(row, col_to_idx(COL["regular_price"]))))

    def eb_fail(res):
        log_revise(service, res, title=title, row=row_num, source="scheduled", action=log_action)
        err = f"{res.get('error_kind')} {res.get('error_code')}: {res.get('message', '')[:150]}"
        if res.get("error_kind") == "auth":
            if sched.set_last_error(item_id, err):
                _notify(f"🚨 <b>eBay token rejected</b> ({html.escape(res.get('error_code') or 'auth')}) "
                        f"— renew EBAY_AUTH_TOKEN in .env. Scheduled {action} for {t} is waiting and "
                        "will retry every 10 min.")
            return f"{item_id}: auth error"
        note = retry_or_give_up(f"eBay {err}")
        return note

    def write_sheet(pairs):
        """Re-resolve the row (an audit delete may have shifted it) and write. Error text or None."""
        try:
            fresh = read_sheet(service, f"'{sheet_name}'!A{start_row}:BB{end_row}")
            m = _rows_by_item_id(fresh, COL, item_id, start_row)
            if len(m) != 1:
                raise RuntimeError(f"item {item_id} now matches {len(m)} sheet rows")
            safe_write_row(service, sheet_name, m[0][0], pairs)
            return None
        except Exception as e:
            logger.error(f"apply_scheduled: sheet write failed for {item_id}: {e}")
            return str(e)

    if action == "end":
        # "Hide listing at sale end": quantity 0 (ReviseInventoryStatus), not EndItem — with Jay's
        # Out of Stock setting + GTC the listing keeps watchers and search rank. Jay raises the
        # quantity by hand to relist (no auto-restore on the PAUSED_MARGIN path).
        res = set_quantity_zero(item_id)
        if not res["ok"]:
            return eb_fail(res)
        err = write_sheet(cost_updates + [(COL["status"], "PAUSED_MARGIN"), (COL["price_change"], "")])
        sched.mark_applied(item_id, res, now=now)
        pop_pending(item_id)
        log_revise(service, {**res, "message": res.get("message", "") +
                             (f" | SHEET WRITE FAILED: {err}" if err else "")},
                   title=title, row=row_num, source="scheduled", action="sale_end_hide")
        _notify(f"⛔ <b>Sale ended — listing hidden (quantity 0)</b>\n{t}\nCostco back to ${price:.2f}. "
                + ("Row marked PAUSED_MARGIN." if not err else
                   f"⚠️ Sheet not updated ({html.escape(err[:120])}) — set status PAUSED_MARGIN by hand."))
        return f"{item_id}: hidden"

    # action == "reprice"
    target = float(a["target_price"])
    live = fetch_item_price(item_id)
    if not live["ok"]:
        return eb_fail({**live, "price": target})
    if live["listing_status"] and live["listing_status"] != "Active":
        sched.cancel_action(item_id)
        _sched_log_line(f"dropped reprice item {item_id} — eBay listing is {live['listing_status']}")
        _notify(f"⚠️ Scheduled reprice dropped — {t} is {html.escape(live['listing_status'])} on eBay.")
        return f"{item_id}: dropped (listing {live['listing_status']})"
    current = live["price"]
    if current >= target - 0.005:
        err = write_sheet(cost_updates + [(COL["ebay_price"], current), (COL["price_change"], "")])
        res = {"ok": True, "item_id": item_id, "price": current,
               "message": f"no revise — live price already >= ${target:.2f}"}
        sched.mark_applied(item_id, res, now=now)
        pop_pending(item_id)
        log_revise(service, res, title=title, row=row_num, old_price=current, source="scheduled")
        _notify(f"✓ Sale ended — {t} is already ${current:.2f} on eBay (≥ ${target:.2f}); nothing revised.")
        return f"{item_id}: already at target"
    res = revise_fixed_price(item_id, target)
    if not res["ok"]:
        return eb_fail(res)
    err = write_sheet(cost_updates + [(COL["ebay_price"], target), (COL["price_change"], "")])
    sched.mark_applied(item_id, res, now=now)
    pop_pending(item_id)
    log_revise(service, {**res, "message": res.get("message", "") +
                         (f" | SHEET WRITE FAILED: {err}" if err else "")},
               title=title, row=row_num, old_price=current, source="scheduled")
    _notify(f"✓ <b>Sale ended — repriced to ${target:.2f}</b> (was ${current:.2f})\n{t}\n"
            f"Costco ${float(a.get('old_cost') or 0):.2f} → ${price:.2f}."
            + ("" if not err else f"\n⚠️ Sheet not updated ({html.escape(err[:120])}) — set col H to "
               f"{target:.2f} and clear col P by hand."))
    return f"{item_id}: repriced ${current:.2f}→${target:.2f}"


def run_apply_scheduled(config, COL, service, sheet_name, start_row, end_row, now=None):
    """
    apply_scheduled mode (every 10 min, Task WAT-ApplyScheduled):
      1. pre-stage: ACTIVE listings whose Costco sale ends within PRESTAGE_LEAD_HOURS get the
         "✓ Reprice / ⛔ Hide at sale end / Ignore" prompt (once per sale end).
      2. due actions (approved by a tap, apply_at <= now): take the scheduler lock, re-scrape
         each product live, then revise / end on eBay only if the sale is really over.
    Returns Run Log keys, or None when nothing happened (a quiet tick writes no Run Log row and
    never touches Chrome or the scheduler lock).
    """
    now = now or sched.utcnow()
    notes = []

    # ── 1. pre-stage prompts (Chrome-free) ───────────────────────────────────
    sale_ends = sched.load_sale_ends()
    horizon = now + timedelta(hours=sched.PRESTAGE_LEAD_HOURS)
    if any(now < (sched.parse_ts(v.get("end_ts")) or now) <= horizon for v in sale_ends.values()):
        rows = read_sheet(service, f"'{sheet_name}'!A{start_row}:BB{end_row}")
        for p in sched.prestage_candidates(rows, COL, config["categories"], sale_ends,
                                           sched.load_store(), now=now, start_row=start_row):
            if send_reprice_prompt(p, keyboard=schedule_keyboard(p["item_id"], p["target"])):
                sched.record_prompt(p)
                notes.append(f"prompted {p['item_id']} (ends {p['sale_end_ts'][:16]})")
            else:
                logger.warning(f"apply_scheduled: pre-stage prompt for {p['item_id']} not sent — retry next tick")

    # ── 2. due actions ───────────────────────────────────────────────────────
    due = sched.due_actions(now)
    if not due:
        return {"status": "ok", "notes": "; ".join(notes)} if notes else None
    if not _acquire_lock("apply_scheduled"):
        # make_browser can kill/relaunch the agent Chrome — never overlap another Chrome run.
        logger.warning(f"apply_scheduled: {len(due)} due action(s) waiting — scheduler lock busy, "
                       "retrying next tick")
        return {"status": "ok", "notes": "; ".join(notes)} if notes else None
    threshold = float(config["business"].get("price_change_threshold", 0.50))
    status = "ok"
    try:
        with make_browser() as page:
            for a in due:
                try:
                    notes.append(_apply_one(a, page, service, COL, sheet_name, start_row, end_row,
                                            threshold, now))
                except Exception as e:
                    status = "error"
                    logger.exception(f"apply_scheduled: {a.get('item_id')} failed")
                    notes.append(f"{a.get('item_id')}: error {e}")
    except Exception as e:
        status = "error"
        logger.error(f"apply_scheduled: browser failed — due actions kept for next tick: {e}")
        notes.append(f"browser failed: {e}")
    finally:
        _release_lock()
    return {"status": status, "notes": "; ".join(n for n in notes if n)}


# ── Mode: DAILY sweep (1x/day) ────────────────────────────────────────────────

def run_daily_sweep(config, COL, service, sheet_name, start_row, end_row):
    """
    Handles APPROVED, PAUSED_OOS, and PAUSED_MARGIN rows once per day:
    - APPROVED:       verify Costco stock, ensure copy exists -> promote to READY
    - PAUSED_OOS:     check if restocked -> promote to WATCH; a listing the active monitor hid
                      (qty 0, data/.oos_hidden.json) gets its eBay quantity restored -> ACTIVE
                      when still profitable (_restore_oos_listing)
    - PAUSED_MARGIN:  check if margin recovered -> promote to WATCH
    Also promotes PAUSED_SEASONAL/PAUSED_DEMAND rows whose re_eval_date has passed -> PENDING.
    Sends alerts only when something changes.
    """
    from datetime import date as date_type

    business   = config["business"]
    categories = config["categories"]

    # ── Spot price movement check ─────────────────────────────────────────────
    # Fires an alert if gold/silver moved > threshold since the last daily run.
    # 1.5% on gold = ~$45-60/oz — enough to shift margin by 1-2 points.
    try:
        spot_alert = check_spot_movement(gold_threshold_pct=1.5, silver_threshold_pct=2.0)
        if spot_alert:
            from tools.alert_sender import send_alert
            urgency = spot_alert["urgent"]
            subject = (
                "[WAT] Spot price moved significantly — review margins"
                if urgency else
                "[WAT] Spot price update — check WATCH items"
            )
            body = (
                "Metal spot prices have moved past the alert threshold since the last check.\n\n"
                + spot_alert["summary"]
                + "\n\n---\nThis alert fires when gold moves >1.5% or silver >2.0% in a day."
            )
            send_alert(subject, body, urgent=urgency)
            logger.info(f"  Spot movement alert sent (urgent={urgency})")
    except Exception as e:
        logger.warning(f"  Spot movement check failed (non-fatal): {e}")

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    today    = date_type.today()

    ready_items        = []
    newly_paused       = []
    products_need_copy = []
    copy_row_map       = []
    oos_recovered      = []
    oos_restore_lines  = {"restored": [], "needs_you": [], "wait": []}   # hidden-on-OOS restocks
    margin_recovered   = []
    re_eval_promoted   = []

    # ── Pass 1: re_eval_date check (no browser needed) ───────────────────────
    for idx, row in enumerate(all_data):
        if not row:
            continue
        status = safe_get(row, col_to_idx(COL["status"]))
        if status not in ("PAUSED_SEASONAL", "PAUSED_DEMAND"):
            continue

        re_eval_raw = safe_get(row, col_to_idx(COL["re_eval_date"]))
        if not re_eval_raw:
            continue

        try:
            re_eval = date_type.fromisoformat(re_eval_raw.strip())
        except ValueError:
            continue

        if today >= re_eval:
            sheet_row = idx + start_row
            title     = safe_get(row, col_to_idx(COL["title"]))
            write_row_partial(service, sheet_name, sheet_row, [
                (COL["status"],      "PENDING"),
                (COL["re_eval_date"], ""),
                (COL["tier_summary"],       f"Re-eval date reached ({re_eval_raw}) — returned to PENDING for re-research"),
            ])
            re_eval_promoted.append({"title": title, "row": sheet_row})
            logger.info(f"  {status} -> PENDING (re_eval_date reached): {title[:50]}")

    # ── Pass 1b: stale SCORED demotion (no browser needed) ───────────────────
    stale_scored = []
    for idx, row in enumerate(all_data):
        if not row:
            continue
        status = safe_get(row, col_to_idx(COL["status"]))
        if status != "SCORED":
            continue

        last_checked_raw = safe_get(row, col_to_idx(COL["last_checked"]))
        new_status, notes = check_scored_staleness(last_checked_raw)
        if new_status:
            sheet_row = idx + start_row
            title     = safe_get(row, col_to_idx(COL["title"]))
            write_row_partial(service, sheet_name, sheet_row, [
                (COL["status"],       new_status),
                (COL["tier_summary"], notes),
                (COL["last_checked"], run_time),
            ])
            stale_scored.append({"title": title, "row": sheet_row})
            logger.info(f"  SCORED -> PENDING (stale): {title[:50]}")

    # ── Pass 2: Costco scrape for APPROVED / PAUSED_OOS / PAUSED_MARGIN ──────
    if sys.platform != "win32":
        logger.info("Daily sweep: Chrome-dependent scrape skipped on non-Windows.")
    else:
        with make_browser() as page:
            for idx, row in enumerate(all_data):
                sheet_row = idx + start_row
                if not row:
                    continue

                status     = safe_get(row, col_to_idx(COL["status"]))
                costco_url = safe_get(row, col_to_idx(COL["costco_url"]))

                if status not in DAILY_SWEEP_STATUSES:
                    continue
                if not costco_url.startswith("http"):
                    continue

                title       = safe_get(row, col_to_idx(COL["title"]))
                category    = safe_get(row, col_to_idx(COL["category"]))
                seo_title   = safe_get(row, col_to_idx(COL["seo_title"]))
                costco_cost = safe_get(row, col_to_idx(COL["costco_cost"]))
                ebay_price  = safe_get(row, col_to_idx(COL["ebay_price"]))
                fee_rate    = safe_get(row, col_to_idx(COL["fee_rate"]))
                ship_cost   = safe_get(row, col_to_idx(COL["ship_cost"]))

                logger.info(f"Daily sweep: {title[:50]} [{status}]")

                costco_data  = scrape_costco(costco_url, page=page)
                stock_status = costco_data["stock_status"]
                new_price    = costco_data["price"]

                updates = [
                    (COL["stock_status"], stock_status),
                    (COL["last_checked"], run_time),
                ]
                if new_price:
                    updates.append((COL["costco_cost"], new_price))
                    # X/AW too — the sweep used to update G only, leaving a stale sale badge
                    updates += sale_column_updates(
                        COL, costco_data, bool(safe_get(row, col_to_idx(COL["sale_info"]))),
                        bool(safe_get(row, col_to_idx(COL["regular_price"]))))

                if status == "APPROVED":
                    if stock_status == "OUT OF STOCK":
                        updates.append((COL["tier_summary"], "Stock OOS — holding APPROVED until restocked"))
                        logger.info(f"  APPROVED held — OOS")
                    else:
                        if not seo_title:
                            cat_config = categories.get(category, {})
                            products_need_copy.append({
                                "title": title, "category": category,
                                "cost": costco_cost, "sell_price": ebay_price,
                                "site_url": cat_config.get("site_url", ""),
                                "discount_code": business["discount_code"],
                            })
                            copy_row_map.append((sheet_row, title))
                            updates.append((COL["tier_summary"], "Stock OK — generating copy, will promote to READY"))
                            logger.info(f"  Stock OK, copy queued")
                        else:
                            updates.append((COL["status"], "READY"))
                            updates.append((COL["tier_summary"], "Stock verified, copy ready — run ebay_export.py to list"))
                            ready_items.append({"title": title, "row": sheet_row, "has_copy": True})
                            logger.info(f"  APPROVED -> READY")

                elif status == "PAUSED_OOS":
                    _, reason_code, notes = determine_status(
                        status, stock_status, None, False, None,
                    )
                    oos_iid = extract_item_id(safe_get(row, col_to_idx(COL["ebay_listing_url"])))
                    hidden_rec = sched.get_oos_hidden(oos_iid) if oos_iid else None
                    if reason_code == "restock" and hidden_rec:
                        # The monitor hid this listing (qty 0) on OOS — bring it back if profitable
                        new_st, notes, line, kind = _restore_oos_listing(
                            service, row, COL, categories, costco_data, title, category, sheet_row,
                            oos_iid, hidden_rec)
                        if line:
                            oos_restore_lines[kind].append(line)
                        if new_st:
                            updates.append((COL["status"], new_st))
                            sched.pop_oos_hidden(oos_iid)
                            oos_recovered.append({"title": title, "row": sheet_row})
                            logger.info(f"  PAUSED_OOS -> {new_st} (restocked, {kind})")
                        else:
                            logger.info(f"  PAUSED_OOS held — {notes}")
                    elif reason_code == "restock":
                        updates.append((COL["status"], "WATCH"))
                        oos_recovered.append({"title": title, "row": sheet_row})
                        logger.info(f"  PAUSED_OOS -> WATCH (restocked)")
                    updates.append((COL["tier_summary"], notes))

                elif status == "PAUSED_MARGIN":
                    # Recompute margin with latest Costco price
                    margin = None
                    try:
                        p = float(str(ebay_price).replace("$", "").replace(",", ""))
                        c = float(str(new_price or costco_cost).replace("$", "").replace(",", ""))
                        f = float(str(fee_rate).replace("%", "")) / (100 if "%" in str(fee_rate) else 1)
                        s = float(str(ship_cost).replace("$", "").replace(",", "")) if ship_cost else 0
                        if p > 0:
                            margin = (p - c - p * f - s) / p
                    except (ValueError, TypeError):
                        pass

                    _, reason_code, notes = determine_status(
                        status, stock_status, margin, False, None,
                        min_margin=business["min_margin_threshold"],
                    )
                    updates.append((COL["tier_summary"], notes))
                    if reason_code == "margin_recovered":
                        updates.append((COL["status"], "WATCH"))
                        margin_recovered.append({"title": title, "row": sheet_row})
                        logger.info(f"  PAUSED_MARGIN -> WATCH (margin {margin:.1%})")

                write_row_partial(service, sheet_name, sheet_row, updates)
                time.sleep(2)

    # ── Heal stale sale badges on SCORED/WATCH rows (capped; G/X/AW only) ─────
    if sys.platform == "win32":
        try:
            _r = run_sale_refresh(config, COL, service, sheet_name, start_row, end_row,
                                  limit=8, statuses={"SCORED", "WATCH"})
            logger.info(f"  {_r['notes']}")
        except Exception as e:
            logger.warning(f"  sale-refresh pass failed (non-fatal): {e}")

    # ── Copy generation for queued APPROVED products ──────────────────────────
    if products_need_copy:
        logger.info(f"Generating copy for {len(products_need_copy)} APPROVED products...")
        batch_size = business["batch_size"]
        for i in range(0, len(products_need_copy), batch_size):
            batch      = products_need_copy[i:i + batch_size]
            batch_rows = copy_row_map[i:i + batch_size]
            try:
                copy_results = generate_listing_copy(batch)
                for result, (row_num, title) in zip(copy_results, batch_rows):
                    copy_updates = [
                        (COL["seo_title"],    result.get("seo_title", "")),
                        (COL["bullets"],      result.get("bullets", "")),
                        (COL["description"],  result.get("description", "")),
                        (COL["redirect_msg"], result.get("redirect_msg", "")),
                        (COL["meta_desc"],    result.get("meta_desc", "")),
                        (COL["keywords"],     result.get("keywords", "")),
                        (COL["alt_text"],     result.get("alt_text", "")),
                        (COL["status"],       "READY"),
                        (COL["tier_summary"],        "Copy generated, stock OK — run ebay_export.py to list"),
                    ]
                    write_row_partial(service, sheet_name, row_num, copy_updates)
                    ready_items.append({"title": title, "row": row_num, "has_copy": True})
                    logger.info(f"  Copy + READY written for row {row_num}")
                time.sleep(1)
            except Exception as e:
                logger.error(f"Copy generation failed: {e}")

    # ── Alerts ────────────────────────────────────────────────────────────────
    if ready_items:
        send_ready_to_list_alert(ready_items, run_time=run_time)
    if any(oos_restore_lines.values()):
        lines = []
        if oos_restore_lines["restored"]:
            lines.append(f"♻️ <b>Back in stock — restored {len(oos_restore_lines['restored'])} eBay "
                         "listing(s)</b> (now ACTIVE):")
            lines += oos_restore_lines["restored"]
        if oos_restore_lines["needs_you"]:
            lines.append(f"⚠️ <b>Back in stock — {len(oos_restore_lines['needs_you'])} need you</b> "
                         "(moved to WATCH):")
            lines += oos_restore_lines["needs_you"]
        lines += oos_restore_lines["wait"]
        _notify("\n".join(lines))

    changes = len(ready_items) + len(newly_paused) + len(oos_recovered) + len(margin_recovered) + len(re_eval_promoted) + len(stale_scored)
    if changes > 0:
        summary_rows = [
            ("Ready to list (new)",        len(ready_items),      "#007aff"),
            ("Restocked -> WATCH/ACTIVE",   len(oos_recovered),    "#34c759"),
            ("Margin recovered -> WATCH",   len(margin_recovered), "#34c759"),
            ("Re-eval date -> PENDING",     len(re_eval_promoted), "#ff9500"),
            ("Stale SCORED -> PENDING",     len(stale_scored),     "#ff9500"),
            ("Newly paused",                len(newly_paused),     "#ff9500"),
        ]
        send_routine_alert(
            subject="Daily sweep complete",
            summary_rows=summary_rows,
            run_time=run_time,
        )
    else:
        logger.info("Daily sweep: no changes — no email sent.")


# ── Mode: RESEARCH (1x/day) ───────────────────────────────────────────────────

def run_research(config, COL, service, sheet_name, start_row, end_row, category=None, limit=None):
    """
    Delegates to researcher.py for PENDING rows.
    Passes --skip-discovery since discovery runs as a separate earlier step.
    """
    logger.info("Research mode — delegating to researcher.py")
    import subprocess, sys
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "researcher.py"),
           "--skip-discovery"]
    if category:
        cmd += ["--category", category]
    if limit is not None:
        cmd += ["--limit", str(limit)]
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        raise RuntimeError(f"researcher.py exited with code {result.returncode}")


# ── Mode: DISCOVERY (1x/day) ──────────────────────────────────────────────────

def run_discovery(config, COL, service, sheet_name, start_row, end_row, category=None, add_limit=None):
    """
    Finds new Costco products and adds them as PENDING.
    Delegates to researcher.py --discover-only.
    Pass category to limit discovery to a single category.
    Pass add_limit to cap the number of new products added to the sheet.
    """
    logger.info("Discovery mode — running discover-only pass")
    import subprocess, sys
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "researcher.py"), "--discover-only"]
    if category:
        cmd += ["--category", category]
    if add_limit is not None:
        cmd += ["--add-limit", str(add_limit)]
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        logger.error(f"researcher.py --discover-only exited with code {result.returncode}")


# ── Mode: ROTATION digest (weekly) ───────────────────────────────────────────

def run_rotation(config, COL, service, sheet_name, start_row, end_row):
    """
    Computes composite performance scores for all ACTIVE/READY products,
    identifies rotation candidates per category, writes scores + notes to sheet,
    saves to data/rotation_log.json, and sends the weekly digest email.
    """
    from tools.rotation_engine import run_rotation_check, save_rotation_log

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")
    run_date = datetime.now().strftime("%Y-%m-%d")

    logger.info("Running rotation check across all categories...")
    rotation_results = run_rotation_check(
        service, sheet_name, all_data, COL, config, start_row=start_row
    )

    save_rotation_log(rotation_results)

    if rotation_results:
        total_candidates = sum(len(v) for v in rotation_results.values())
        logger.info(f"Rotation candidates: {total_candidates} across {len(rotation_results)} category/categories")
        send_rotation_digest(rotation_results, run_date)
    else:
        logger.info("Rotation check complete — no categories at capacity or all scores healthy.")


# ── Mode: REFRESH-NOTES (one-shot) ───────────────────────────────────────────

def run_refresh_notes(config, COL, service, sheet_name, start_row, end_row):
    """
    Retroactively rewrites Col T first-line summary for rows that have notes but
    are missing the [T header format. Safe to re-run — skips already-formatted rows.

    No eBay calls, no Claude calls. Reads from existing sheet data only.
    Typical runtime: <30 seconds.
    """
    business  = config["business"]
    fee_rate  = business.get("default_fee_rate", 0.1325)

    range_name = f"'{sheet_name}'!A{start_row}:AV{end_row}"
    rows = read_sheet(service, range_name)
    logger.info(f"refresh-notes: read {len(rows)} rows, scanning for old-format notes...")

    updated_count = 0
    for i, row in enumerate(rows):
        sheet_row = start_row + i

        def _get(col_letter):
            idx = col_to_idx(col_letter)
            return str(row[idx]).strip() if idx < len(row) else ""

        tier_sum = _get(COL["tier_summary"])
        if not tier_sum or tier_sum.startswith("[T"):
            continue   # empty or already has new format — skip

        # Extract data from existing row
        score_str  = _get(COL["demand_score"])
        costco_url = _get(COL["costco_url"])
        sugg_str   = _get(COL["suggested_price"])
        cost_str   = _get(COL["costco_cost"])

        # Derive tier from score
        tier = "?"
        try:
            sc   = float(score_str)
            tier = "1" if sc >= 6.0 else ("2" if sc >= 3.0 else "3")
        except (ValueError, TypeError):
            pass

        # Build price+margin summary
        price_summary = ""
        if sugg_str:
            try:
                sp     = float(sugg_str.replace("$", "").replace(",", ""))
                cost_f = float(cost_str.replace("$", "").replace(",", ""))
                net_f  = sp - cost_f - sp * fee_rate
                price_summary = f"Sugg: ${sp:,.2f} | ~{net_f / sp * 100:.1f}% margin | "
            except (ValueError, TypeError):
                price_summary = f"Sugg: {sugg_str} | "

        url_part = f"Costco: {costco_url}" if costco_url else "Costco: (see Col R)"
        summary_line = f"[T{tier} | Score {score_str} | {price_summary}{url_part}]"

        write_row_partial(service, sheet_name, sheet_row, [(COL["tier_summary"], summary_line)])
        updated_count += 1
        logger.info(f"  Row {sheet_row}: header added ({score_str} / {tier})")

    logger.info(f"refresh-notes: updated {updated_count} rows.")


# ── Mode: RESCORE (one-shot) ──────────────────────────────────────────────────

def run_rescore(config, COL, service, sheet_name, start_row, end_row):
    """
    Bulk re-score: flip WATCH + PAUSED_DEMAND rows back to PENDING so the next
    research run re-scores them with the current scoring logic (net $/unit +
    monthly-profit override). Clears re_eval_date. One-shot helper — run after a
    scoring change so items parked by the old logic re-surface for review.
    """
    status_i = col_to_idx(COL["status"])
    title_i = col_to_idx(COL["title"])
    reeval_i = col_to_idx(COL["re_eval_date"]) if "re_eval_date" in COL else None

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")
    flipped = []
    for idx, row in enumerate(all_data):
        if not row:
            continue
        status = safe_get(row, status_i)
        if status not in ("WATCH", "PAUSED_DEMAND"):
            continue
        sheet_row = idx + start_row
        title = safe_get(row, title_i)
        updates = [(COL["status"], "PENDING")]
        if reeval_i is not None:
            updates.append((COL["re_eval_date"], ""))
        write_row_partial(service, sheet_name, sheet_row, updates)
        flipped.append((sheet_row, title))
        logger.info(f"  {status} -> PENDING (re-score): {title[:50]}")

    logger.info(f"rescore: {len(flipped)} row(s) -> PENDING")
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        _send_telegram(
            token, chat_id,
            f"🔁 Re-score: {len(flipped)} item(s) moved WATCH/PAUSED_DEMAND → PENDING. "
            f"Run research to re-score them with the new logic."
        )
    return {"status": "ok", "notes": f"rescore: {len(flipped)} row(s) -> PENDING"}


# ── Mode: RECHECK-AUDIT (one-shot) ────────────────────────────────────────────
#
# Refresh Costco cost + stock for every AUDIT_REVIEW row so Jay can make
# keep/archive decisions on fresh numbers instead of stale last_checked data.
# Writes G (cost), F (stock), O (last_checked); net (I) recalculates in-sheet.

def run_recheck_audit(config, COL, service, sheet_name, start_row, end_row):
    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AW{end_row}")
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    status_i = col_to_idx(COL["status"])
    url_i = col_to_idx(COL["costco_url"])
    title_i = col_to_idx(COL["title"])

    targets = []
    for idx, row in enumerate(all_data):
        if not row:
            continue
        if safe_get(row, status_i) != "AUDIT_REVIEW":
            continue
        if not safe_get(row, url_i).startswith("http"):
            continue
        targets.append((idx + start_row, row))

    if not targets:
        logger.info("recheck-audit: no AUDIT_REVIEW rows with a Costco URL — nothing to refresh.")
        return

    logger.info(f"recheck-audit: refreshing cost/stock for {len(targets)} AUDIT_REVIEW rows.")
    checked = 0
    with make_browser() as page:
        for sheet_row, row in targets:
            title = safe_get(row, title_i)
            costco_url = safe_get(row, url_i)
            logger.info(f"  recheck-audit: {title[:50]}...")
            checked += 1

            costco_data = scrape_costco(costco_url, page=page)
            new_price = costco_data["price"]
            stock_status = costco_data["stock_status"]
            image_urls = " | ".join(costco_data["image_urls"])
            if costco_data.get("error"):
                logger.warning(f"    Scrape error: {costco_data.get('error')}")

            updates = [
                (COL["stock_status"], stock_status),
                (COL["last_checked"], run_time),
            ]
            if new_price:
                updates.append((COL["costco_cost"], new_price))
            if image_urls:
                updates.append((COL["image_urls"], image_urls))
            write_row_partial(service, sheet_name, sheet_row, updates)
            time.sleep(2)

    logger.info(f"recheck-audit: done — refreshed {checked} rows.")


# ── Mode: RECHECK (one-shot) ──────────────────────────────────────────────────

def run_recheck(config, COL, service, sheet_name, start_row, end_row, force=False):
    """
    Full data-fill pass for rows with missing or failed data:
      - CHECK FAILED stock status  →  re-scrape Costco
      - Blank costco_cost (G)      →  re-scrape Costco
      - Blank ebay_price (H)       →  run eBay comps, write suggested price
      - Blank avg_price (L)        →  run eBay comps, write eBay market data

    force=True: re-run Costco AND eBay on ALL products regardless of current data.
    Use this for a full sheet refresh (e.g. after column restructure).

    eBay comps run independently of Costco — if Costco fails, eBay data still gets
    written. G/H/L/K must all be populated for Jordan to review any product.
    """
    import random as random  # already used below for sleep jitter
    from tools.costco_scraper import refresh_session
    from tools.ebay_research import get_ebay_comps

    def _suggest_price(cost_s, ebay_data, fee_rate):
        """Price suggestion: median sold → median active → avg sold → cost×1.30 fallback.
        Always returns a price when cost is known — Col H must never be blank."""
        try:
            cost = float(str(cost_s).replace("$", "").replace(",", ""))
        except (ValueError, TypeError):
            return None
        if cost <= 0:
            return None

        anchor = (
            ebay_data.get("median_sold")
            or ebay_data.get("median_active")
            or ebay_data.get("avg_sold_price")
        )
        if anchor:
            try:
                price = float(anchor)
                if price < cost * 0.80:
                    logger.warning(
                        f"  recheck _suggest_price: eBay anchor ${price:.2f} < 80% of cost ${cost:.2f}. "
                        "Negative margin or wrong product match — writing market price, flagging."
                    )
                    ebay_data["wrong_product_flag"] = True
                # No margin floor — market price is the price. Col I (net_profit) shows reality.
                price = min(price, cost * 3.0)     # cap at 3× cost (clearly bad data)
                return round(price) - 0.01
            except (ValueError, TypeError):
                pass
        # No eBay data — cost-based fallback so Col H is never left blank
        return round(cost * 1.30) - 0.01

    categories = config["categories"]
    all_data   = read_sheet(service, f"'{sheet_name}'!A{start_row}:AU{end_row}")
    run_time   = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Identify rows needing a recheck
    targets = []
    for idx, row in enumerate(all_data):
        if not row or not row[0]:
            continue
        status     = safe_get(row, col_to_idx(COL["status"]))
        stock      = safe_get(row, col_to_idx(COL["stock_status"]))
        costco_url = safe_get(row, col_to_idx(COL["costco_url"]))
        cost       = safe_get(row, col_to_idx(COL["costco_cost"]))
        ebay_price = safe_get(row, col_to_idx(COL["ebay_price"]))
        avg_price  = safe_get(row, col_to_idx(COL["avg_price"]))
        title      = safe_get(row, col_to_idx(COL["title"]))
        category   = safe_get(row, col_to_idx(COL["category"]))

        if not costco_url.startswith("http"):
            continue
        if not force and status in ("ACTIVE", "READY"):
            continue

        if force:
            needs_costco = True
            needs_ebay   = True
        else:
            needs_costco = "CHECK FAILED" in stock or not cost
            needs_ebay   = not ebay_price or not avg_price

        if needs_costco or needs_ebay:
            targets.append({
                "sheet_row":   idx + start_row,
                "row":         row,
                "title":       title,
                "category":    category,
                "costco_url":  costco_url,
                "status":      status,
                "needs_costco": needs_costco,
                "needs_ebay":  needs_ebay,
                "cost":        cost,
                "ebay_price":  ebay_price,
            })

    if not targets:
        logger.info("recheck: no rows need retrying — G/H/L all populated.")
        return

    costco_fail = [t for t in targets if t["needs_costco"]]
    ebay_fill   = [t for t in targets if t["needs_ebay"]]
    logger.info(
        f"recheck: {len(targets)} rows targeted — "
        f"{len(costco_fail)} need Costco re-scrape, "
        f"{len(ebay_fill)} need eBay price data."
    )

    fixed        = 0
    still_costco = []

    with make_browser() as page:
        # ── Pass 1 + 2: Costco re-scrape for failed/missing cost ─────────────
        for attempt_round in range(2):
            if attempt_round > 0 and still_costco:
                logger.info("recheck: pass 2 — session refresh, retrying Costco failures...")
                refresh_session(page)
                targets_this_pass = still_costco
                still_costco = []
            else:
                targets_this_pass = [t for t in targets if t["needs_costco"]]

            for _idx_t, t in enumerate(targets_this_pass):
                # Refresh session every 10 products to prevent Costco rate-limiting
                # on long runs (cookies stay valid but session activity resets the timeout).
                if _idx_t > 0 and _idx_t % 10 == 0:
                    logger.info(f"  [Costco] session refresh after {_idx_t} products...")
                    refresh_session(page)
                    time.sleep(random.uniform(3, 6))

                logger.info(f"  [Costco] row {t['sheet_row']}: {t['title'][:50]}")
                costco_data  = scrape_costco(t["costco_url"], page=page)
                stock_status = costco_data["stock_status"]
                new_price    = costco_data["price"]

                if stock_status == "CHECK FAILED":
                    still_costco.append(t)
                    logger.warning(f"    still failing: {t['title'][:40]}")
                    time.sleep(3)
                    continue

                on_sale      = costco_data.get("on_sale", False)
                sale_savings = costco_data.get("sale_savings")
                sale_expires = costco_data.get("sale_expires")
                free_ship    = costco_data.get("free_shipping", False)

                sale_val = ""
                if on_sale:
                    sale_val = f"🔥 -${sale_savings:.0f}" if sale_savings else "🔥 SALE"
                    if sale_expires:
                        sale_val += f" ends {sale_expires}"

                updates = [
                    (COL["stock_status"],  stock_status),
                    (COL["last_checked"],  run_time),
                    (COL["sale_info"],     sale_val),
                    (COL["free_shipping"], "✓ FREE" if free_ship else ""),
                ]
                if new_price:
                    updates.append((COL["costco_cost"], new_price))
                    t["cost"] = str(new_price)   # update for eBay pass below

                write_row_partial(service, sheet_name, t["sheet_row"], updates)
                if sale_val:
                    orig = costco_data.get("original_price")
                    log_sale(service, t["title"], t["category"], new_price or t.get("cost"),
                             f"{orig:.2f}" if orig else "", sale_val,
                             coupon_type=costco_data.get("coupon_type") or "",
                             coupon_label=costco_data.get("coupon_label") or "")
                logger.info(f"    Costco OK: {stock_status} | ${new_price}")
                t["needs_costco"] = False
                time.sleep(2)

        # ── eBay comps pass: fill missing H/L/K/M for ALL rows that need it ────
        # Run eBay comps regardless of whether Costco succeeded or failed —
        # eBay data is independent and Col H/L must always be populated.
        # For Costco-failed rows: use existing cost from the sheet if available.
        ebay_targets = [t for t in targets if t["needs_ebay"]]

        logger.info(f"recheck: running eBay comps for {len(ebay_targets)} rows with missing price data...")

        for t in ebay_targets:
            title    = t["title"]
            category = t["category"]
            cost_s   = t["cost"]
            row      = t["row"]

            logger.info(f"  [eBay] row {t['sheet_row']}: {title[:50]}")

            cat_config = categories.get(category, {})
            fee_rate   = cat_config.get("fee_rate", 0.1325)
            brand      = None
            model      = None

            try:
                ebay_data = get_ebay_comps(
                    title, category, page=page,
                    brand=brand, model=model,
                    ebay_category_id=cat_config.get("ebay_category_id"),
                )
            except Exception as e:
                logger.warning(f"    eBay comps failed: {e}")
                time.sleep(5)
                continue

            updates = [(COL["last_checked"], run_time)]

            # Always write eBay market data regardless of whether we suggest a price
            if ebay_data.get("sold_90d") is not None:
                updates.append((COL["sold_90d"],   ebay_data["sold_90d"]))
            if ebay_data.get("avg_sold_price") is not None:
                updates.append((COL["avg_price"],  ebay_data["avg_sold_price"]))
            if ebay_data.get("active_count") is not None:
                updates.append((COL["comp_count"], ebay_data["active_count"]))

            # Suggested price — write to H and V if not already set
            suggested = _suggest_price(cost_s, ebay_data, fee_rate)
            if suggested:
                if not t["ebay_price"]:
                    updates.append((COL["ebay_price"], suggested))
                sugg_idx = col_to_idx(COL["suggested_price"])
                if not safe_get(row, sugg_idx):
                    updates.append((COL["suggested_price"], suggested))

            # Update tier_summary to show new data is filled
            sold   = ebay_data.get("sold_90d", "?")
            avg    = ebay_data.get("avg_sold_price", "?")
            active = ebay_data.get("active_count", "?")
            sugg_s = f" | Sugg: ${suggested:.2f}" if suggested else ""
            updates.append((COL["tier_summary"],
                            f"[Rechecked {run_time}] sold90d={sold} avgeBay=${avg} active={active}{sugg_s}"))

            write_row_partial(service, sheet_name, t["sheet_row"], updates)
            fixed += 1
            logger.info(f"    eBay OK: sold={sold} avg=${avg} sugg={suggested}")
            time.sleep(random.uniform(3, 5))

    still_fail_count = len(still_costco)
    logger.info(
        f"recheck complete: {fixed} eBay rows filled | "
        f"{still_fail_count} Costco rows still failing."
    )
    if still_costco:
        logger.warning(
            "Still failing: " + ", ".join(t["title"][:30] for t in still_costco[:5])
            + (" ..." if len(still_costco) > 5 else "")
        )


# ── Cookie freshness check ────────────────────────────────────────────────────

_COOKIES_PATH     = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "data", "costco_cookies.json")
_COOKIE_WARN_DAYS = 25
_COOKIE_WARN_TS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "data", ".cookie_warn_ts")
_COOKIE_WARN_INTERVAL_SEC = 7 * 86400  # don't re-warn within 7 days

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Fire-and-forget Telegram message. Logs on failure, never raises. True iff it was sent."""
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req     = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram message sent.")
        return True
    except Exception as e:
        logger.warning(f"Telegram message failed (non-fatal): {e}")
        return False


def run_sale_digest(config, COL, service, sheet_name, start_row, end_row, dry_run=False) -> dict:
    """
    sale-digest mode ("Sale Radar"): ONE Telegram message listing every tracked item that is
    really on sale (tools.sale_digest filters out the stale/false-positive col X badges).
    Read-only, no Chrome. Silent when nothing is on sale. dry_run prints instead of sending.
    Returns Run Log keys (status/notes/errors).
    """
    rows = read_sheet(service, f"'{sheet_name}'!A{start_row}:AW{end_row}")
    now = datetime.now()
    items, skipped = select_sale_items(rows, COL, now=now, start_row=start_row)
    message = format_digest(items, top=10, now=now,
                            warn_hours=int(config["business"].get("sale_warn_hours", 48)))
    skipped_txt = ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())) or "none"
    notes = (f"{'[dry-run] ' if dry_run else ''}{len(items)} on sale (shown {min(len(items), 10)}); "
             f"ignored badges: {skipped_txt}")
    logger.info(f"sale-digest: {notes}")
    result = {"status": "ok", "notes": notes}
    if message is None:
        return result                      # nothing on sale -> silent
    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # cp1252 can't print 🛒
        except Exception:
            pass
        print(message)
        return result
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat_id) or not _send_telegram(token, chat_id, message):
        result["status"] = "error"
        result["errors"] = "Sale Radar not delivered: Telegram not configured or send failed"
    return result


EXPORT_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "data", ".export_state.json")


def _send_telegram_document(token: str, chat_id: str, path, caption: str) -> bool:
    """sendDocument (multipart) with an HTML caption. Never raises. True iff it was sent."""
    import uuid
    boundary = f"wat{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts = []
    for name, value in (("chat_id", chat_id), ("caption", caption), ("parse_mode", "HTML")):
        parts += [b"--" + boundary.encode(), f'Content-Disposition: form-data; name="{name}"'.encode(),
                  b"", value.encode("utf-8")]
    try:
        data = open(path, "rb").read()
    except OSError as e:
        logger.warning(f"Telegram document not sent — can't read {path}: {e}")
        return False
    parts += [b"--" + boundary.encode(),
              f'Content-Disposition: form-data; name="document"; filename="{os.path.basename(path)}"'.encode(),
              b"Content-Type: text/csv", b"", data, b"--" + boundary.encode() + b"--", b""]
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument",
                                 data=crlf.join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=30)
        logger.info("Telegram document sent.")
        return True
    except Exception as e:
        logger.warning(f"Telegram document failed (non-fatal): {e}")
        return False


def _load_export_state() -> dict:
    try:
        with open(EXPORT_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_export_state(state: dict) -> None:
    tmp = EXPORT_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, EXPORT_STATE_PATH)


def run_export(config, COL, service, sheet_name, start_row, end_row, dry_run=False) -> dict:
    """
    export mode: the three listing steps chained, so Jay never runs them by hand.
      1. photo_compositor — branded main photo for APPROVED/READY rows with image + SKU
      2. upload_photos    — host new/changed photos on eBay (data/hosted_photos.json)
      3. ebay_export      — Seller Hub CSV of READY rows to data/exports/
    A failure in 1 or 2 never blocks the CSV (export falls back to the col AT images).
    Silent with 0 READY rows. The CSV is written + sent (Telegram document) only when its
    content differs from the last export (data/.export_state.json), so READY rows waiting to be
    listed don't produce a new file and alert every day. dry_run: no writes, no eBay calls,
    no CSV, no Telegram — only counts. Returns Run Log keys (status/notes/errors).
    """
    import hashlib
    from tools import ebay_export, photo_compositor, upload_photos

    tag = "[dry-run] " if dry_run else ""
    notes, errors = [], []
    rows = read_sheet(service, f"'{sheet_name}'!A{start_row}:AZ{end_row}")

    # 1. Photos
    try:
        tpl = None if dry_run else photo_compositor.load_template()
        jobs = photo_compositor.build_jobs(rows, start_row, photo_compositor._load_columns())
        ps = photo_compositor.run(jobs, tpl, dry_run=dry_run)
        notes.append(f"photos {'would make' if dry_run else 'made'} {ps['processed']}, "
                     f"have {ps['done']}, no SKU {ps['no_sku']}, no image {ps['no_image']}"
                     + (f", failed {len(ps['failed'])}" if ps["failed"] else ""))
        errors += [f"photo {s}: {e}" for s, e in ps["failed"]]
    except FileNotFoundError as e:
        notes.append("photos skipped — template missing")
        logger.warning(f"export: {e}")
    except Exception as e:
        errors.append(f"photo step crashed: {e}")
        logger.exception("export: photo step failed — continuing")

    # 2. Upload
    try:
        us = upload_photos.run(dry_run=dry_run)
        n_up = us["planned"] if dry_run else us["uploaded"]
        notes.append(f"upload {'would host' if dry_run else 'hosted'} {n_up}, unchanged {us['skipped']}"
                     + (f", failed {len(us['failed'])}" if us["failed"] else ""))
        errors += [f"upload {s}: {m}" for s, m in us["failed"]]
    except Exception as e:
        errors.append(f"upload step crashed: {e}")
        logger.exception("export: upload step failed — continuing")

    # 3. CSV
    eligible = ebay_export.select_eligible(rows, start_row)
    result = {"status": "ok"}
    if not eligible:
        notes.append("0 READY — nothing to export")
    elif dry_run:
        notes.append(f"would export {len(eligible)} READY row(s)")
    else:
        csv_text = ebay_export.generate_ebay_csv(eligible, ebay_export._load_config())
        n = max(len(csv_text.strip().splitlines()) - 1, 0)    # rows that survived the export's skips
        digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
        state = _load_export_state()
        if n == 0:
            notes.append(f"{len(eligible)} READY but all skipped by export (no price/category/already listed)")
        elif state.get("sha256") == digest:
            notes.append(f"{n} listing(s) unchanged since {state.get('file', 'last export')} — not re-sent")
        else:
            path = ebay_export.write_csv(csv_text)
            hosted = upload_photos.load_hosted()
            branded = sum(1 for _r, row in eligible
                          if upload_photos.hosted_url_for(
                              ebay_export._photo_key(ebay_export._safe(row, ebay_export._COL["sku"])), hosted))
            notes.append(f"exported {n} listing(s) -> {path.name} ({branded} with branded photo)")
            caption = (f"📦 Export ready — {n} listing(s). CSV: {html.escape(path.name)}. "
                       f"Upload to Seller Hub.\nBranded main photo: {branded}/{len(eligible)}")
            if errors:
                caption += f"\n⚠️ {len(errors)} photo/upload problem(s) — see Run Log"
            token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
            sent = bool(token and chat_id) and (_send_telegram_document(token, chat_id, path, caption)
                                                or _send_telegram(token, chat_id, caption))
            if sent:
                _save_export_state({"sha256": digest, "file": path.name, "rows": n,
                                    "at": datetime.now().isoformat(timespec="seconds")})
            else:
                result["status"] = "error"
                errors.append("export alert not delivered: Telegram not configured or send failed")

    result["notes"] = tag + "; ".join(notes)
    if errors:
        result["errors"] = "; ".join(errors)[:600]
        if result["status"] == "ok" and any("crashed" in e or "not delivered" in e for e in errors):
            result["status"] = "error"
    logger.info(f"export: {result['notes']}")
    return result


SALE_REFRESH_STATUSES = {"SCORED", "WATCH", "READY", "APPROVED", "PAUSED_MARGIN", "PAUSED_OOS"}


def run_sale_refresh(config, COL, service, sheet_name, start_row, end_row,
                     limit=12, statuses=None, blank_too=False) -> dict:
    """
    sale-refresh mode: heal unverified col X sale badges on non-ACTIVE rows (the active monitor
    only covers ACTIVE; pre-fix rows hold DOM false-positives like "-$100" on a $15 item).
    Candidates: status in `statuses`, Costco URL, and EITHER a badge that isn't verified
    (tools.sale_monitor.badge_verified) OR — when blank_too=True — a blank badge, so rows never
    scraped get their real sale end date backfilled too. Re-scrapes up to `limit` (0 = no cap)
    and writes ONLY cols G / X / AW (no status change, no last_checked — that would defer
    SCORED->PENDING staleness, no alerts). Verified badges stop being candidates, so repeated
    runs converge. Needs Chrome (local only).
    """
    statuses = SALE_REFRESH_STATUSES if statuses is None else statuses
    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AW{end_row}")
    now = datetime.now()

    targets = []
    for idx, row in enumerate(all_data):
        if not row:
            continue
        if safe_get(row, col_to_idx(COL["status"])) not in statuses:
            continue
        if not str(safe_get(row, col_to_idx(COL["costco_url"]))).startswith("http"):
            continue
        badge = safe_get(row, col_to_idx(COL["sale_info"]))
        if not badge:
            if not blank_too:
                continue
        elif badge_verified(badge, safe_get(row, col_to_idx(COL["costco_cost"])),
                            safe_get(row, col_to_idx(COL["regular_price"])), now):
            continue
        targets.append((idx + start_row, row))
    total = len(targets)
    if limit:
        targets = targets[:limit]
    if not targets:
        return {"status": "ok", "notes": "sale-refresh: no unverified badges"}
    if sys.platform != "win32":
        return {"status": "ok", "notes": f"sale-refresh: {total} candidates, Chrome scrape skipped (non-Windows)"}

    from tools.costco_scraper import refresh_session
    still_on, cleared, failed = 0, 0, 0
    with make_browser() as page:
        for n, (sheet_row, row) in enumerate(targets):
            if n and n % 10 == 0:
                refresh_session(page)
            title = safe_get(row, col_to_idx(COL["title"]))
            logger.info(f"  [sale-refresh] row {sheet_row}: {title[:50]}")
            data = scrape_costco(safe_get(row, col_to_idx(COL["costco_url"])), page=page)
            if data.get("stock_status") == "CHECK FAILED" or not data.get("price"):
                failed += 1
                time.sleep(3)
                continue
            updates = [(COL["costco_cost"], data["price"])] + sale_column_updates(
                COL, data, True, bool(safe_get(row, col_to_idx(COL["regular_price"]))))
            write_row_partial(service, sheet_name, sheet_row, updates)
            if data.get("on_sale"):
                still_on += 1
                orig = data.get("original_price")
                log_sale(service, title, safe_get(row, col_to_idx(COL["category"])), data["price"],
                         f"{orig:.2f}" if orig else "", updates[1][1],
                         coupon_type=data.get("coupon_type") or "",
                         coupon_label=data.get("coupon_label") or "")
            else:
                cleared += 1
            time.sleep(2)
    notes = (f"sale-refresh: {len(targets)} of {total} candidates — {still_on} on sale, "
             f"{cleared} badges cleared, {failed} scrape failures")
    logger.info(notes)
    return {"status": "ok", "notes": notes}


SAVINGS_ALERT_STATE = os.path.join(_REPO_ROOT, "data", ".savings_alert.json")
SAVINGS_SEEN_STATE = os.path.join(_REPO_ROOT, "data", ".savings_seen.json")


def _append_pending_rows(service, sheet_name, products, COL):
    """Append PENDING rows via the researcher's discovery writer (imported lazily — it is heavy)."""
    from agents.researcher import _add_new_products_batch
    _add_new_products_batch(service, sheet_name, products, COL)


def run_savings(config, COL, service, sheet_name, start_row, end_row,
                dry_run=False, limit=None, add_limit=None) -> dict:
    """
    savings mode: find sales across Costco and cross-reference them with the sheet. Two sources
    (tools.costco_savings): the Member-Only Savings page (~186 cards) and the full "OFF" search
    listing (~1,586 items, crawled page by page — `business.savings.search_pages`, 0 = off).
      TRACKED row  -> re-verify on the product page (price API), write G / X / AW only. Never touches
                      status or col P. A sale we did not already have (no badge, or price/end changed)
                      goes into ONE consolidated Telegram message.
      NEW item     -> hard-filtered so the research queue is never flooded: it must classify into one of
                      our categories (search items by Costco's category path, page cards by title
                      keywords), show a discount in the listing, clear `min_discount_pct`, not have been
                      rejected within `reject_cooldown_days`, and then be CONFIRMED on sale by the price
                      API. Appended as PENDING with G/AW/X pre-filled, at most `add_limit` per run (and
                      only while PENDING rows < `max_pending`, when that is set).
    `limit` caps product pages opened per run (tracked first, then new by discount). dry_run scrapes
    but writes / alerts / logs nothing and prints what it WOULD do. Needs Chrome (Windows only).
    """
    biz = config["business"].get("savings") or {}
    limit = limit or int(biz.get("scrape_limit", 40))
    add_limit = add_limit or int(biz.get("add_limit", 10))
    min_pct = float(biz.get("min_discount_pct") or 0)
    cooldown = int(biz.get("reject_cooldown_days") or 0)
    max_pending = int(biz.get("max_pending") or 0)
    search_pages = int(biz.get("search_pages") or 0)
    tag = "[dry-run] " if dry_run else ""
    if sys.platform != "win32":
        return {"status": "ok", "notes": "savings: skipped (non-Windows — needs Chrome)"}

    from tools.costco_scraper import refresh_session
    kw_rules = costco_savings.compile_keywords(biz.get("keywords"))
    path_rules = costco_savings.compile_keywords(biz.get("paths"))
    rows = costco_savings.load_rows(
        read_sheet(service, f"'{sheet_name}'!A{start_row}:AW{end_row}"), COL, start_row)
    existing_titles = {r["norm_title"] for r in rows}
    existing_pids = {r["product_id"] for r in rows if r["product_id"]}
    pending_now = sum(1 for r in rows if r["status"] == "PENDING")
    room = add_limit if not max_pending else max(0, min(add_limit, max_pending - pending_now))
    if not dry_run:
        try:
            ensure_grid_columns(service, sheet_name, required_grid_columns(COL))
        except Exception as e:
            logger.warning(f"  grid check failed (non-fatal): {e}")
    alert_state = load_alert_state(SAVINGS_ALERT_STATE)
    seen = costco_savings.load_seen(SAVINGS_SEEN_STATE)
    rejected = {}                                   # pid -> reason, persisted after the run
    now = datetime.now()

    n = {"updated": 0, "unchanged": 0, "added": 0, "failed": 0, "over_budget": 0,
         "no_hint": 0, "below_min": 0, "cooldown": 0, "backlog": 0, "not_on_sale": 0, "dup": 0}
    entries, keys, new_products, plan = [], [], [], []

    with make_browser() as page:
        dom_items, banner_end = costco_savings.scrape_savings_listing(
            page, biz.get("url") or costco_savings.SAVINGS_URL)
        search_items, smeta = [], {}
        if search_pages > 0:
            search_items, smeta = costco_savings.scrape_search_listing(
                page, biz.get("search_url") or costco_savings.SEARCH_URL,
                max_pages=search_pages, refresh=refresh_session)
        items = costco_savings.merge_items(search_items, dom_items)
        if not items:
            msg = (f"savings: 0 items from the savings page and {len(search_items)} from the search listing — "
                   f"Costco layout changed or the site blocked us")
            logger.error(msg)
            token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
            if not dry_run and token and chat_id:
                _send_telegram(token, chat_id, f"⚠️ {msg}")
            return {"status": "error", "errors": msg}

        matches = costco_savings.match_items(items, rows)
        new_in, off_category = [], 0
        for it in matches["new"]:
            cat = costco_savings.classify_item(it, kw_rules, path_rules)
            if not cat:
                off_category += 1
                continue
            pct = costco_savings.discount_pct(it.get("list_sale"), it.get("list_regular"))
            if not costco_savings.has_discount_hint(it):
                n["no_hint"] += 1                   # the listing itself shows no markdown
            elif min_pct and pct and pct < min_pct:
                n["below_min"] += 1                 # priced hint says the markdown is trivial
            elif costco_savings.recently_rejected(seen, it["product_id"], now, cooldown):
                n["cooldown"] += 1                  # the price API rejected it within the cooldown
            else:
                new_in.append((it, cat))
        if room == 0 and new_in:
            n["backlog"] = len(new_in)              # PENDING backlog at max_pending: add nothing, scrape nothing
            new_in = []
        new_in.sort(key=lambda t: costco_savings.savings_rank(t[0]), reverse=True)
        queue = ([("tracked", it, row) for it, row, _ in matches["tracked"]]
                 + [("new", it, cat) for it, cat in new_in])
        n["over_budget"] = max(0, len(queue) - limit)
        queue = queue[:limit]

        for pos, (kind, item, ref) in enumerate(queue):
            if kind == "new" and len(new_products) >= room:
                n["over_budget"] += 1
                continue
            if pos and pos % 10 == 0:
                refresh_session(page)
            logger.info(f"  [savings] {kind}: {item['title'][:60]}")
            data = scrape_costco(item["url"], page=page)
            time.sleep(2)
            if data.get("stock_status") == "CHECK FAILED" or not data.get("price"):
                n["failed"] += 1
                continue
            if not data.get("on_sale"):
                n["not_on_sale"] += 1          # listing text was only a hint; the price API disagrees
                if kind == "new":
                    rejected[item["product_id"]] = "not_on_sale"
                continue
            fallback_end = item.get("promo_end") or (banner_end if item.get("source") != "search" else None)
            if not data.get("sale_expires") and fallback_end:
                data = {**data, "sale_expires": fallback_end}
            price, orig = data["price"], data.get("original_price")
            coupon = dict(coupon_type=data.get("coupon_type") or "",
                          coupon_label=data.get("coupon_label") or "")

            if kind == "tracked":
                row = ref
                old_cost, old_badge = to_float(row["costco_cost"]), row["sale_info"]
                old_regular = to_float(row["regular_price"])
                updates = [(COL["costco_cost"], price)] + sale_column_updates(
                    COL, data, bool(old_badge), bool(row["regular_price"]))
                badge = updates[1][1]
                old_end, new_end = parse_sale_expiry(old_badge, now), parse_sale_expiry(badge, now)
                is_new_sale = (not old_badge or old_cost is None or abs(old_cost - price) > 0.005
                               or (old_end is not None and new_end is not None
                                   and old_end.date() != new_end.date()))
                changed = (old_cost is None or abs(old_cost - price) > 0.005 or badge != old_badge
                           or (orig or None) != old_regular)
                if not changed:
                    n["unchanged"] += 1
                    continue
                plan.append(f"UPDATE row {row['row_num']}: {row['title'][:50]} — G {row['costco_cost'] or '-'}→{price}, "
                            f"X '{badge}', AW {orig or '-'}{'  [NEW SALE]' if is_new_sale else ''}")
                if not dry_run:
                    safe_write_row(service, sheet_name, row["row_num"], updates)
                    log_sale(service, row["title"], row["category"], price,
                             f"{orig:.2f}" if orig else "", badge, **coupon)
                    time.sleep(0.3)
                n["updated"] += 1
                # A never-priced PENDING row (blank G) has no "before": we record the sale but a
                # "new sale" alert would be a guess, and search coverage makes these frequent.
                if is_new_sale and not (old_cost is None and row["status"] == "PENDING"):
                    key = f"{row['title']}|{price:.2f}|{new_end:%Y-%m-%d}" if new_end else f"{row['title']}|{price:.2f}|-"
                    if not already_alerted(alert_state, key):
                        entries.append({"title": row["title"], "price": price, "regular": orig,
                                        "end": data.get("sale_expires"),
                                        "net": costco_savings.net_profit(row, price), "new_row": False})
                        keys.append(key)
            else:
                cat = ref
                title = data.get("title") or item["title"]
                if (costco_savings._norm_title(title) in existing_titles
                        or costco_savings.product_id(item["url"]) in existing_pids):
                    n["dup"] += 1              # the full title turned out to be a row we already have
                    continue
                savings_amt = data.get("sale_savings")
                pct = costco_savings.discount_pct(price, orig or (price + savings_amt if savings_amt else None))
                if min_pct and pct < min_pct:
                    n["below_min"] += 1        # confirmed on sale, but too small a markdown to research
                    rejected[item["product_id"]] = "below_min"
                    continue
                updates = sale_column_updates(COL, data, False, False)
                badge = updates[0][1]
                leaf = " > ".join(item.get("category_path", "").split(" > ")[-2:])[:70]   # the leaf is what triage needs
                where = (f"Costco savings search: {leaf}"
                         if item.get("source") == "search" else "Member-Only Savings page")
                new_products.append({"title": title, "category": cat, "url": item["url"], "price": price,
                                     "sale_info": badge, "regular_price": orig or "",
                                     "tier_summary": f"Discovered via {where} — awaiting research"})
                existing_titles.add(costco_savings._norm_title(title))
                plan.append(f"ADD PENDING [{cat}] ({item.get('source', 'page')}, {pct:g}% off): {title[:60]} — ${price}"
                            f"{f' (was ${orig:.2f})' if orig else ''} X '{badge}'")
                entries.append({"title": title, "price": price, "regular": orig,
                                "end": data.get("sale_expires"), "net": None, "new_row": True})
                keys.append(f"{title}|{price:.2f}|new")
                if not dry_run:
                    log_sale(service, title, cat, price, f"{orig:.2f}" if orig else "", badge, **coupon)

    if new_products and not dry_run:
        _append_pending_rows(service, sheet_name, new_products, COL)
    n["added"] = len(new_products)
    if rejected and not dry_run:
        costco_savings.save_seen(SAVINGS_SEEN_STATE, seen, rejected, now)

    message = costco_savings.format_alert(entries)
    result = {"status": "ok", "new_products": n["added"]}
    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # cp1252 can't print 🔔
        except Exception:
            pass
        print("\n".join(plan) or "(nothing would be written)")
        print("--- Telegram message ---")
        print(message or "(none — no new sales)")
    elif message:
        token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if token and chat_id and _send_telegram(token, chat_id, message):
            record_alerts(alert_state, keys, path=SAVINGS_ALERT_STATE)
        else:
            result["status"] = "error"
            result["errors"] = "savings alert not delivered: Telegram not configured or send failed"

    new_total = len(matches["new"])
    dropped = n["no_hint"] + n["below_min"] + n["cooldown"] + n["backlog"] + n["not_on_sale"] + n["dup"]
    src = f"{len(dom_items)} page + {len(search_items)} search"
    if search_pages > 0:
        src += f" [search {smeta.get('pages', 0)} pages" + (f", {smeta['stopped']}" if smeta.get("stopped") else "") + "]"
    notes = (f"{tag}savings: found {len(items)} ({src}) — tracked {len(matches['tracked'])}, "
             f"new {new_total} (in-category {new_total - off_category}, off-category {off_category}), "
             f"dropped {dropped} (no_hint {n['no_hint']}, below_min {n['below_min']}, cooldown {n['cooldown']}, "
             f"backlog {n['backlog']}, not_on_sale {n['not_on_sale']}, dup {n['dup']}); "
             f"held back {len(matches['possible'])} possible + {len(matches['ambiguous'])} ambiguous; "
             f"updated {n['updated']}, unchanged {n['unchanged']}, added {n['added']}, "
             f"alerted {len(entries)}, failed {n['failed']}, over budget {n['over_budget']}")
    logger.info(notes)
    result["notes"] = notes
    return result

def run_ebay_sync_mode(config, COL, service, sheet_name, start_row, end_row, dry_run=False) -> dict:
    """
    ebay_sync mode: fetch eBay listings, write units_sold, return Run Log keys
    (status/notes/errors). Losing-money breaches on ACTIVE rows are auto-repriced to
    break-even on eBay inside run_ebay_sync (business.ebay_sync.auto_reprice). Telegram
    fires ONLY when tools.ebay_sync built an alert (auto-reprice outcomes, losing-money
    margin breach, ACTIVE-but-not-on-eBay, or a rejected auth token) or, at most once a
    day, a thin-margin digest — silent otherwise.
    """
    result = ebay_sync.run_ebay_sync(config, COL, service, sheet_name, start_row,
                                     end_row, dry_run=dry_run)
    messages = [m for m in (result.pop("alert", None), result.pop("digest", None)) if m]
    if messages:
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if token and chat_id:
            for m in messages:
                _send_telegram(token, chat_id, m)
        else:
            logger.warning("ebay_sync: alert not sent — TELEGRAM_BOT_TOKEN/CHAT_ID unset")
    return result


def _attempt_cookie_autorefresh(reason: str = "age") -> tuple[bool, str]:
    """
    Thin wrapper around tools.cookie_refresh.refresh_costco_cookies() (the
    shared subprocess/throttle mechanics, also called from
    tools/costco_scraper.py's expiry-based trigger) — this module just owns
    sending its own success alert, tagged with which trigger fired
    (reason: "age" or "expiry").
    Returns (success, diagnostic), same contract as before.
    """
    ok, diag = refresh_costco_cookies()
    if ok:
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if token and chat_id:
            _send_telegram(token, chat_id, f"✅ Costco cookies auto-refreshed and synced to VPS. ({reason})")
    return ok, diag


def _check_cookie_age() -> None:
    """
    Triggers on either (a) costco_cookies.json being 25+ days old, or (b) too
    many cookies inside it being expired (same rule as costco_scraper — Costco
    cookies expire in days, so file age alone never catches this).
    First tries to auto-refresh the cookies (see _attempt_cookie_autorefresh,
    24h-throttled and shared with the scraper's own expiry trigger);
    only sends a Telegram message telling Jordan to do it manually (if
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set) when that fails, at most
    once per _COOKIE_WARN_INTERVAL_SEC (7 days), so a stale cookie file
    doesn't re-alert on every scheduled run. Never blocks the run.
    """
    if not os.path.exists(_COOKIES_PATH):
        return  # no cookies file — rotation/refresh-notes mode, or first run

    age_days = (time.time() - os.path.getmtime(_COOKIES_PATH)) / 86400
    expired, total = cookie_expiry_stats(_COOKIES_PATH)
    age_trigger    = age_days >= _COOKIE_WARN_DAYS
    expiry_trigger = expiry_refresh_needed(expired, total)
    if not age_trigger and not expiry_trigger:
        return

    if age_trigger:
        reason_text = f"Costco cookies are {age_days:.0f} days old (warn threshold: {_COOKIE_WARN_DAYS} days)."
    else:
        reason_text = f"{expired}/{total} Costco cookies are expired (file is only {age_days:.0f} days old)."

    autorefreshed, autorefresh_diag = _attempt_cookie_autorefresh("age" if age_trigger else "expiry")
    if autorefreshed:
        return

    last_warn_ts = 0.0
    if os.path.exists(_COOKIE_WARN_TS_PATH):
        try:
            with open(_COOKIE_WARN_TS_PATH) as f:
                last_warn_ts = float(f.read().strip())
        except Exception:
            last_warn_ts = 0.0
    if time.time() - last_warn_ts < _COOKIE_WARN_INTERVAL_SEC:
        logger.info(f"{reason_text} Warning already sent within last 7 days, skipping.")
        return

    subject = "⚠️ Costco cookies need refresh — run .\\run.ps1 cookies on your laptop"
    body    = (
        f"{reason_text}\n\n"
        f"Scraping will likely start returning CHECK FAILED soon.\n\n"
    )
    if autorefresh_diag:
        # truncated in _tail_output() before this escape, per feedback_telegram_html_escaping
        body += f"Auto-refresh attempted and failed:\n{html.escape(autorefresh_diag)}\n\n"
    body += (
        f"To fix:\n"
        f"  1. On your Windows laptop: .\\run.ps1 cookies\n"
        f"  2. Upload to VPS:          python tools/cookie_sync.py upload"
    )
    logger.warning(f"{reason_text} — {subject}")

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        tg_text = f"<b>{subject}</b>\n\n{body}"
        _send_telegram(token, chat_id, tg_text)

    try:
        os.makedirs(os.path.dirname(_COOKIE_WARN_TS_PATH), exist_ok=True)
        with open(_COOKIE_WARN_TS_PATH, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        logger.warning(f"Cookie warn timestamp write failed (non-fatal): {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

LOCK_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", ".scheduler_lock"
)
LOCK_STALE_MINUTES = 45


def _acquire_lock(mode: str) -> bool:
    if os.path.exists(LOCK_FILE):
        age_min = (time.time() - os.path.getmtime(LOCK_FILE)) / 60
        if age_min < LOCK_STALE_MINUTES:
            try:
                held_by = open(LOCK_FILE).read().strip()
            except Exception:
                held_by = "?"
            logger.warning(f"Scheduler [{mode}] skipped — another run in progress "
                            f"(started {held_by}, {age_min:.0f}m ago), exiting.")
            return False
        logger.warning(f"Scheduler [{mode}]: lock is {age_min:.0f}m old — "
                        f"previous run likely crashed. Reclaiming.")

    with open(LOCK_FILE, "w") as f:
        f.write(f"{mode} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return True


def _release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def _main_apply_scheduled():
    config, COL = load_config(), load_col_map()
    business = config["business"]
    service = None
    start = log_run_start("apply_scheduled")
    try:
        service = get_sheets_service()
        result = run_apply_scheduled(config, COL, service, business["sheet_name"],
                                     business["data_start_row"], business["data_end_row"])
    except Exception as e:
        logger.error(f"Scheduler [apply_scheduled] failed: {e}")
        result = {"status": "error", "errors": traceback.format_exc()[-600:]}
        _notify(f"💥 Scheduler [apply_scheduled] CRASHED — {html.escape(str(e)[:200])}")
    if result:
        logger.info(f"apply_scheduled: {result.get('notes') or result.get('errors', '')}")
        log_run_end("apply_scheduled", start, result, service)
    else:
        logger.debug("apply_scheduled: nothing due")


def main():
    parser = argparse.ArgumentParser(description="Costco -> eBay Monitoring Agent")
    parser.add_argument(
        "--mode",
        choices=["active", "daily", "research", "discovery", "rotation", "refresh-notes", "recheck", "recheck-audit", "rescore", "audit", "ebay_sync",
                 "sale-digest", "sale-refresh", "savings", "apply_scheduled", "export"],
        default="active",
        help=(
            "active:         Check ACTIVE listings for stock/price changes (3x/day)\n"
            "daily:          Verify APPROVED stock, promote to READY, check PAUSED_OOS (1x/day)\n"
            "research:       Score PENDING products via researcher.py (1x/day)\n"
            "discovery:      Find new Costco products, add as PENDING (1x/day)\n"
            "rotation:       Score all active products, flag underperformers, send weekly digest (1x/week)\n"
            "refresh-notes:  Retroactively reformat Col T summary line (one-shot)\n"
            "recheck:        Retry Costco scrape for CHECK FAILED and empty-price rows (one-shot)\n"
            "rescore:        Flip WATCH/PAUSED_DEMAND -> PENDING for re-scoring (one-shot)\n"
            "audit:          Graveyard pass — remove junk, flag borderline rows (every 2 days)\n"
            "ebay_sync:      Sync eBay active listings -> units_sold; flag price mismatch / removed listings\n"
            "sale-digest:    ONE Telegram message of tracked items really on sale (read-only)\n"
            "sale-refresh:   Re-scrape non-ACTIVE rows with unverified sale badges (G/X/AW only)\n"
            "savings:        Scrape Costco Member-Only Savings -> update tracked sales, alert, add new PENDING rows\n"
            "apply_scheduled: every 10 min — pre-stage sale-end prompts; apply approved reprice/Hide at sale end\n"
            "export:         branded photos -> host on eBay -> Seller Hub CSV of READY rows (Telegram on change)"
        ),
    )
    parser.add_argument("--category", type=str, default=None,
                        help="Limit research/discovery to one category (e.g. 'Jewelry')")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit research to N products (for testing); savings: max product pages opened")
    parser.add_argument("--force", action="store_true",
                        help="(recheck only) Re-run Costco + eBay on ALL products, not just missing-data rows")
    parser.add_argument("--add-limit", type=int, default=None,
                        help="Max new products to add to sheet during discovery / savings")
    parser.add_argument("--row", type=int, default=None,
                        help="(active only) Check just this sheet row — live testing")
    parser.add_argument("--dry-run", action="store_true",
                        help="(ebay_sync / sale-digest / savings / export) Report without writing / print instead of sending")
    parser.add_argument("--queue", action="store_true",
                        help="(sale-refresh only) Scrape SCORED + AUDIT_REVIEW rows, including blank sale badges")
    args = parser.parse_args()

    if args.mode == "apply_scheduled":
        # Runs every 10 min: takes the scheduler lock itself, and only when an action is due;
        # no cookie check, and no Run Log row for a quiet tick.
        _main_apply_scheduled()
        return

    if not _acquire_lock(args.mode):
        return

    if args.mode not in ("sale-digest", "export"):   # Chrome-free: a cookie refresh/alert is irrelevant
        _check_cookie_age()

    config     = load_config()
    COL        = load_col_map()
    business   = config["business"]
    sheet_name = business["sheet_name"]
    start_row  = business["data_start_row"]
    end_row    = business["data_end_row"]

    logger.info(f"Scheduler [{args.mode}] started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    _run_start   = log_run_start(args.mode)
    _run_results = {"status": "ok"}
    service      = None  # defined here so finally block can always reference it

    try:
        service = get_sheets_service()
        if args.mode == "active":
            run_active_monitor(config, COL, service, sheet_name, start_row, end_row,
                               only_rows={args.row} if args.row else None)
        elif args.mode == "daily":
            run_daily_sweep(config, COL, service, sheet_name, start_row, end_row)
        elif args.mode == "research":
            run_research(config, COL, service, sheet_name, start_row, end_row,
                         category=args.category, limit=args.limit)
        elif args.mode == "audit":
            run_audit(config, COL, service, sheet_name, start_row, end_row)
        elif args.mode == "discovery":
            run_discovery(config, COL, service, sheet_name, start_row, end_row,
                          category=args.category, add_limit=args.add_limit)
        elif args.mode == "rotation":
            run_rotation(config, COL, service, sheet_name, start_row, end_row)
        elif args.mode == "refresh-notes":
            run_refresh_notes(config, COL, service, sheet_name, start_row, end_row)
        elif args.mode == "recheck":
            run_recheck(config, COL, service, sheet_name, start_row, end_row,
                        force=args.force)
        elif args.mode == "recheck-audit":
            run_recheck_audit(config, COL, service, sheet_name, start_row, end_row)
        elif args.mode == "ebay_sync":
            _run_results.update(run_ebay_sync_mode(config, COL, service, sheet_name,
                                                   start_row, end_row, dry_run=args.dry_run))
        elif args.mode == "sale-digest":
            _run_results.update(run_sale_digest(config, COL, service, sheet_name,
                                                start_row, end_row, dry_run=args.dry_run))
        elif args.mode == "sale-refresh":
            if args.queue:
                _run_results.update(run_sale_refresh(
                    config, COL, service, sheet_name, start_row, end_row,
                    limit=args.limit or 0, statuses={"SCORED", "AUDIT_REVIEW"},
                    blank_too=True))
            else:
                _run_results.update(run_sale_refresh(config, COL, service, sheet_name,
                                                     start_row, end_row, limit=args.limit or 12))
        elif args.mode == "savings":
            _run_results.update(run_savings(config, COL, service, sheet_name, start_row, end_row,
                                            dry_run=args.dry_run, limit=args.limit,
                                            add_limit=args.add_limit))
        elif args.mode == "export":
            _run_results.update(run_export(config, COL, service, sheet_name, start_row, end_row,
                                           dry_run=args.dry_run))
        elif args.mode == "rescore":
            _run_results.update(run_rescore(config, COL, service, sheet_name, start_row, end_row))
        # One alert per run if the Costco price API stopped returning prices (col G would
        # otherwise freeze silently, as it did after the Sep 2026 redesign), and one if the
        # inventory API stopped giving a delivery state (stock falls back to the ATC guess).
        for _miss in (price_miss_message(), inventory_miss_message()):
            if _miss:
                logger.error(_miss)
                _tok, _chat = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
                if _tok and _chat:
                    _send_telegram(_tok, _chat, _miss)
    except Exception as e:
        _run_results["status"] = "error"
        _run_results["errors"] = traceback.format_exc()[-600:]
        logger.error(f"Scheduler [{args.mode}] failed: {e}")
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if token and chat_id:
            _send_telegram(token, chat_id, f"💥 Scheduler [{args.mode}] CRASHED — {str(e)[:200]}")
        raise
    finally:
        _release_lock()
        log_run_end(args.mode, _run_start, _run_results, service)
        # Heartbeat ping — tells healthchecks.io this run completed successfully
        _hc_key = f"HEALTHCHECK_URL_{args.mode.upper().replace('-', '_')}"
        _hc_url = os.getenv(_hc_key)
        if _hc_url and _run_results["status"] == "ok":
            try:
                import urllib.request
                urllib.request.urlopen(_hc_url, timeout=5)
                logger.info(f"Heartbeat ping sent ({_hc_key})")
            except Exception as e:
                logger.warning(f"Heartbeat ping FAILED ({_hc_key}): {e}")
        # Run summary email (skip for active monitor — already handled by send_urgent_alert)
        if args.mode != "active" and not args.dry_run:
            try:
                send_run_summary(args.mode, _run_results)
            except Exception as e:
                logger.warning(f"Run summary email failed: {e}")

    logger.info(f"Scheduler [{args.mode}] complete.")


if __name__ == "__main__":
    main()
