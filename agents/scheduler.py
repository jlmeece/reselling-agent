"""
Costco -> eBay Monitoring Agent
================================
WAT Framework: Agent layer for monitoring and status management.

Four run modes (--mode flag):
  active    3x/day  ACTIVE listings — stock/price, reprice alerts, URGENT SMS
  daily     1x/day  APPROVED->READY (copy+stock verify), PAUSED_OOS stock check
  research  1x/day  PENDING rows — full research + scoring (calls researcher.py logic)
  discovery 1x/day  Find new Costco products, add as PENDING

Run locally:  python agents/scheduler.py --mode active
Run in cloud: GitHub Actions handles scheduling (.github/workflows/run_agent.yml)
"""

import argparse
import os
import sys
import time
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
from tools.alert_sender import send_urgent_alert, send_routine_alert, send_ready_to_list_alert, send_rotation_digest


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

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AZ{end_row}")
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    urgent_items = []
    checked = 0

    # Pre-scan: skip Chrome entirely if there are no ACTIVE/READY-with-URL rows.
    # This prevents a guaranteed crash on CI (GitHub Actions) where no local Chrome exists.
    has_active = any(
        safe_get(r, col_to_idx(COL["status"])) in ACTIVE_MONITOR_STATUSES
        or (safe_get(r, col_to_idx(COL["status"])) == "READY"
            and safe_get(r, col_to_idx(COL["ebay_listing_url"])).startswith("http"))
        for r in all_data if r
    )
    if not has_active:
        logger.info("Active monitor: no ACTIVE listings found — skipping browser launch.")
        return

    with make_browser() as page:
        for idx, row in enumerate(all_data):
            sheet_row = idx + start_row
            if not row:
                continue

            status    = safe_get(row, col_to_idx(COL["status"]))
            ebay_url  = safe_get(row, col_to_idx(COL["ebay_listing_url"]))

            # Only process ACTIVE rows (or READY rows that may have an eBay URL now)
            if status not in ACTIVE_MONITOR_STATUSES and not (status == "READY" and ebay_url.startswith("http")):
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
                (COL["notes"],         notes),
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

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AZ{end_row}")
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
                (COL["notes"],       f"Re-eval date reached ({re_eval_raw}) — returned to PENDING for re-research"),
            ])
            re_eval_promoted.append({"title": title, "row": sheet_row})
            logger.info(f"  {status} -> PENDING (re_eval_date reached): {title[:50]}")

    # ── Pass 2: Costco scrape for APPROVED / PAUSED_OOS / PAUSED_MARGIN ──────
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
                    updates.append((COL["notes"], "Stock OOS — holding APPROVED until restocked"))
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
                        updates.append((COL["notes"], "Stock OK — generating copy, will promote to READY"))
                        logger.info(f"  Stock OK, copy queued")
                    else:
                        updates.append((COL["status"], "READY"))
                        updates.append((COL["notes"], "Stock verified, copy ready — run ebay_export.py to list"))
                        ready_items.append({"title": title, "row": sheet_row, "has_copy": True})
                        logger.info(f"  APPROVED -> READY")

            elif status == "PAUSED_OOS":
                _, reason_code, notes = determine_status(
                    status, stock_status, None, False, None,
                )
                updates.append((COL["notes"], notes))
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
                updates.append((COL["notes"], notes))
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
                        (COL["notes"],        "Copy generated, stock OK — run ebay_export.py to list"),
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

def run_research(config, COL, service, sheet_name, start_row, end_row, category=None):
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
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        logger.error(f"researcher.py exited with code {result.returncode}")


# ── Mode: DISCOVERY (1x/day) ──────────────────────────────────────────────────

def run_discovery(config, COL, service, sheet_name, start_row, end_row, category=None):
    """
    Finds new Costco products and adds them as PENDING.
    Delegates to researcher.py --discover-only.
    Pass category to limit discovery to a single category.
    """
    logger.info("Discovery mode — running discover-only pass")
    import subprocess, sys
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "researcher.py"), "--discover-only"]
    if category:
        cmd += ["--category", category]
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

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AZ{end_row}")
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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Costco -> eBay Monitoring Agent")
    parser.add_argument(
        "--mode",
        choices=["active", "daily", "research", "discovery", "rotation"],
        default="active",
        help=(
            "active:    Check ACTIVE listings for stock/price changes (3x/day)\n"
            "daily:     Verify APPROVED stock, promote to READY, check PAUSED_OOS (1x/day)\n"
            "research:  Score PENDING products via researcher.py (1x/day)\n"
            "discovery: Find new Costco products, add as PENDING (1x/day)\n"
            "rotation:  Score all active products, flag underperformers, send weekly digest (1x/week)"
        ),
    )
    parser.add_argument("--category", type=str, default=None,
                        help="Limit research/discovery to one category (e.g. 'Jewelry')")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit research to N products (for testing)")
    args = parser.parse_args()

    config     = load_config()
    COL        = load_col_map()
    business   = config["business"]
    service    = get_sheets_service()
    sheet_name = business["sheet_name"]
    start_row  = business["data_start_row"]
    end_row    = business["data_end_row"]

    logger.info(f"Scheduler [{args.mode}] started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    if args.mode == "active":
        run_active_monitor(config, COL, service, sheet_name, start_row, end_row)
    elif args.mode == "daily":
        run_daily_sweep(config, COL, service, sheet_name, start_row, end_row)
    elif args.mode == "research":
        run_research(config, COL, service, sheet_name, start_row, end_row,
                     category=args.category)
    elif args.mode == "discovery":
        run_discovery(config, COL, service, sheet_name, start_row, end_row,
                      category=args.category)
    elif args.mode == "rotation":
        run_rotation(config, COL, service, sheet_name, start_row, end_row)

    logger.info(f"Scheduler [{args.mode}] complete.")


if __name__ == "__main__":
    main()
