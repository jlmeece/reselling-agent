"""
Costco -> eBay Monitoring Agent
================================
WAT Framework: Agent layer for monitoring and status management.

Five run modes (--mode flag):
  active    3x/day  ACTIVE listings — stock/price, reprice alerts, URGENT SMS
  daily     1x/day  APPROVED->READY (copy+stock verify), PAUSED_OOS stock check
  research  1x/day  PENDING rows — full research + scoring (calls researcher.py logic)
  discovery 1x/day  Find new Costco products, add as PENDING
  audit     every 2 days  Graveyard pass — remove junk, flag borderline rows

Run locally:  python agents/scheduler.py --mode active
Run in cloud: GitHub Actions handles scheduling (.github/workflows/run_agent.yml)
"""

import argparse
import json
import os
import sys
import time
import traceback
import urllib.request
import yaml
from datetime import datetime
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import get_sheets_service, read_sheet, write_row_partial
from tools.costco_scraper import scrape_costco, make_browser
from tools.status_logic import (
    determine_status, suggest_reprice,
    ACTIVE_MONITOR_STATUSES, DAILY_SWEEP_STATUSES, SKIP_STATUSES,
)
from tools.listing_copy import generate_listing_copy
from tools.alert_sender import send_urgent_alert, send_routine_alert, send_ready_to_list_alert, send_rotation_digest, send_run_summary, send_sale_expiry_alert
from tools.run_logger import log_run_start, log_run_end
from tools.spot_price import check_spot_movement
from agents.auditor import run_audit


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

def run_active_monitor(config, COL, service, sheet_name, start_row, end_row):
    """
    Checks all ACTIVE listings every run.
    - Detects stock changes -> PAUSED_OOS
    - Detects price changes -> reprice suggestion in URGENT alert
    - Detects margin erosion -> PAUSED_MARGIN
    - Auto-promotes READY->ACTIVE when eBay URL is filled
    - Sends URGENT email+SMS if any action needed, otherwise stays silent
    """
    business = config["business"]
    categories = config["categories"]

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    urgent_items = []
    checked = 0

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

            # APPROVED rows: only check stock — no margin/demand logic needed yet
            if is_approved_check:
                updates = [
                    (COL["stock_status"], stock_status),
                    (COL["last_checked"], run_time),
                ]
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

            # Detect price change
            price_changed = False
            if new_price and costco_cost:
                try:
                    old = float(str(costco_cost).replace("$", "").replace(",", ""))
                    if abs(new_price - old) > business["price_change_threshold"]:
                        price_changed = True
                        logger.info(f"  Price: ${old} -> ${new_price}")
                except (ValueError, TypeError):
                    pass

            # Compute margin inline (avoid formula column)
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

            try:
                demand_int = int(demand) if demand else None
            except (ValueError, TypeError):
                demand_int = None

            new_status, reason_code, notes = determine_status(
                status, stock_status, margin, price_changed, demand_int,
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

            # Build updates
            updates = [
                (COL["stock_status"],  stock_status),
                (COL["last_checked"],  run_time),
                (COL["tier_summary"],         notes),
                (COL["image_urls"],    image_urls),
                (COL["price_change"],  "YES — update listing" if price_changed else ""),
            ]
            if new_price:
                updates.append((COL["costco_cost"], new_price))
            if new_status != status:
                updates.append((COL["status"], new_status))
                logger.info(f"  Status: {status} -> {new_status}")

            write_row_partial(service, sheet_name, sheet_row, updates)

            # Collect items needing action
            if reason_code != "ok" and reason_code != "ebay_url_detected":
                reprice_note = ""
                if price_changed and new_price:
                    try:
                        f_rate = float(str(fee_rate).replace("%", "")) / (100 if "%" in str(fee_rate) else 1)
                        s_cost = float(str(ship_cost).replace("$", "").replace(",", "")) if ship_cost else 0
                        suggested = suggest_reprice(new_price, f_rate, s_cost)
                        if suggested:
                            reprice_note = f"Suggested new eBay price: ${suggested:.2f}"
                    except (ValueError, TypeError):
                        pass

                urgent_items.append({
                    "title":        title,
                    "row":          sheet_row,
                    "category":     category,
                    "reason":       notes,
                    "reprice_note": reprice_note,
                })

            time.sleep(2)

    logger.info(f"Active monitor complete. Checked: {checked} | Urgent: {len(urgent_items)}")

    # ── Sale expiry check ──────────────────────────────────────────────────────
    # Online arbitrage model — no inventory held. Sale expiry = repricing event.
    import re as _re
    from datetime import datetime as _dt

    SALE_WARN_HOURS   = int(business.get("sale_warn_hours",   48))
    SALE_URGENT_HOURS = int(business.get("sale_urgent_hours", 24))

    expiring = []
    for row in all_data:
        if not row:
            continue
        status    = safe_get(row, col_to_idx(COL["status"]))
        sale_info = safe_get(row, col_to_idx(COL["sale_info"]))
        if status != "ACTIVE" or not sale_info:
            continue

        exp_match = _re.search(r'ends?\s+(\d{1,2}/\d{1,2}/\d{2,4})', sale_info, _re.IGNORECASE)
        if not exp_match:
            continue

        try:
            exp_str = exp_match.group(1)
            exp_dt = None
            for fmt in ("%m/%d/%y", "%m/%d/%Y"):
                try:
                    exp_dt = _dt.strptime(exp_str, fmt).replace(hour=23, minute=59)
                    break
                except ValueError:
                    continue
            if exp_dt is None:
                continue

            hours_left = (exp_dt - _dt.now()).total_seconds() / 3600
            if 0 < hours_left <= SALE_WARN_HOURS:
                savings_match = _re.search(r'\$(\d+\.?\d*)', sale_info)
                savings = float(savings_match.group(1)) if savings_match else None
                costco_cost_raw = safe_get(row, col_to_idx(COL["costco_cost"]))
                try:
                    regular_cost = (float(costco_cost_raw) + savings) if (costco_cost_raw and savings) else None
                except Exception:
                    regular_cost = None

                expiring.append({
                    "title":               safe_get(row, col_to_idx(COL["title"])),
                    "sale_expires":        exp_str,
                    "sale_savings":        savings,
                    "costco_url":          safe_get(row, col_to_idx(COL["costco_url"])),
                    "ebay_url":            safe_get(row, col_to_idx(COL["ebay_listing_url"])),
                    "current_ebay_price":  safe_get(row, col_to_idx(COL["ebay_price"])),
                    "costco_cost":         costco_cost_raw,
                    "regular_costco_cost": regular_cost,
                    "fee_rate":            safe_get(row, col_to_idx(COL["fee_rate"])),
                    "net_profit":          safe_get(row, col_to_idx(COL["net_profit"])),
                    "hours_left":          hours_left,
                })
        except Exception as e:
            logger.debug(f"  Sale expiry parse error: {e}")

    if expiring:
        min_hours = min(p["hours_left"] for p in expiring)
        send_sale_expiry_alert(expiring, hours_remaining=min_hours)
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


# ── Mode: DAILY sweep (1x/day) ────────────────────────────────────────────────

def run_daily_sweep(config, COL, service, sheet_name, start_row, end_row):
    """
    Handles APPROVED, PAUSED_OOS, and PAUSED_MARGIN rows once per day:
    - APPROVED:       verify Costco stock, ensure copy exists -> promote to READY
    - PAUSED_OOS:     check if restocked -> promote to WATCH
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
                    updates.append((COL["tier_summary"], notes))
                    if reason_code == "restock":
                        updates.append((COL["status"], "WATCH"))
                        oos_recovered.append({"title": title, "row": sheet_row})
                        logger.info(f"  PAUSED_OOS -> WATCH (restocked)")

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

    changes = len(ready_items) + len(newly_paused) + len(oos_recovered) + len(margin_recovered) + len(re_eval_promoted)
    if changes > 0:
        summary_rows = [
            ("Ready to list (new)",        len(ready_items),      "#007aff"),
            ("Restocked -> WATCH",          len(oos_recovered),    "#34c759"),
            ("Margin recovered -> WATCH",   len(margin_recovered), "#34c759"),
            ("Re-eval date -> PENDING",     len(re_eval_promoted), "#ff9500"),
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
            tier = "1" if sc >= 7.0 else ("2" if sc >= 4.0 else "3")
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


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    """Fire-and-forget Telegram message. Logs on failure, never raises."""
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req     = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram cookie-age warning sent.")
    except Exception as e:
        logger.warning(f"Telegram message failed (non-fatal): {e}")


def _check_cookie_age() -> None:
    """
    Warns if costco_cookies.json is older than 25 days.
    Sends email alert and Telegram message (if TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID are set). Never blocks the run.
    """
    if not os.path.exists(_COOKIES_PATH):
        return  # no cookies file — rotation/refresh-notes mode, or first run

    age_days = (time.time() - os.path.getmtime(_COOKIES_PATH)) / 86400
    if age_days < _COOKIE_WARN_DAYS:
        return

    subject = "⚠️ Costco cookies need refresh — run .\\run.ps1 cookies on your laptop"
    body    = (
        f"Costco cookies are {age_days:.0f} days old (warn threshold: {_COOKIE_WARN_DAYS} days).\n\n"
        f"Scraping will likely start returning CHECK FAILED soon.\n\n"
        f"To fix:\n"
        f"  1. On your Windows laptop: .\\run.ps1 cookies\n"
        f"  2. Upload to VPS:          python tools/cookie_sync.py upload"
    )
    logger.warning(f"Cookie age: {age_days:.0f} days — {subject}")

    try:
        from tools.alert_sender import send_alert
        send_alert(subject, body, urgent=False)
    except Exception as e:
        logger.warning(f"Cookie age email failed (non-fatal): {e}")

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        tg_text = f"<b>{subject}</b>\n\n{body}"
        _send_telegram(token, chat_id, tg_text)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Costco -> eBay Monitoring Agent")
    parser.add_argument(
        "--mode",
        choices=["active", "daily", "research", "discovery", "rotation", "refresh-notes", "recheck", "audit"],
        default="active",
        help=(
            "active:         Check ACTIVE listings for stock/price changes (3x/day)\n"
            "daily:          Verify APPROVED stock, promote to READY, check PAUSED_OOS (1x/day)\n"
            "research:       Score PENDING products via researcher.py (1x/day)\n"
            "discovery:      Find new Costco products, add as PENDING (1x/day)\n"
            "rotation:       Score all active products, flag underperformers, send weekly digest (1x/week)\n"
            "refresh-notes:  Retroactively reformat Col T summary line (one-shot)\n"
            "recheck:        Retry Costco scrape for CHECK FAILED and empty-price rows (one-shot)\n"
            "audit:          Graveyard pass — remove junk, flag borderline rows (every 2 days)"
        ),
    )
    parser.add_argument("--category", type=str, default=None,
                        help="Limit research/discovery to one category (e.g. 'Jewelry')")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit research to N products (for testing)")
    parser.add_argument("--force", action="store_true",
                        help="(recheck only) Re-run Costco + eBay on ALL products, not just missing-data rows")
    parser.add_argument("--add-limit", type=int, default=None,
                        help="Max new products to add to sheet during discovery")
    args = parser.parse_args()
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
            run_active_monitor(config, COL, service, sheet_name, start_row, end_row)
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
    except Exception as e:
        _run_results["status"] = "error"
        _run_results["errors"] = traceback.format_exc()[-600:]
        logger.error(f"Scheduler [{args.mode}] failed: {e}")
        raise
    finally:
        log_run_end(args.mode, _run_start, _run_results, service)
        # Heartbeat ping — tells healthchecks.io this run completed successfully
        _hc_key = f"HEALTHCHECK_URL_{args.mode.upper()}"
        _hc_url = os.getenv(_hc_key)
        if _hc_url and _run_results["status"] == "ok":
            try:
                import urllib.request
                urllib.request.urlopen(_hc_url, timeout=5)
                logger.debug(f"Heartbeat ping sent ({_hc_key})")
            except Exception:
                pass
        # Run summary email (skip for active monitor — already handled by send_urgent_alert)
        if args.mode != "active":
            try:
                send_run_summary(args.mode, _run_results)
            except Exception as e:
                logger.warning(f"Run summary email failed: {e}")

    logger.info(f"Scheduler [{args.mode}] complete.")


if __name__ == "__main__":
    main()
