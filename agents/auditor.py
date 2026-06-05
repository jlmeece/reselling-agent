"""
Sheet Auditor — CFO Mode
========================
Runs every other day via scheduler.py --mode audit.
Reviews all non-protected rows and:
  - Auto-removes rows with clearly broken economics
  - Flags borderline rows as AUDIT_REVIEW for manual decision
  - Writes removed rows to Graveyard tab (append-only)
  - Appends summary row to Audit Log tab
  - Queues substitute placeholder PENDING rows for removed products
  - Sends Telegram notification on completion
"""

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime

from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.sheet_writer import get_sheets_service, read_sheet, write_row_partial
from tools.graveyard_writer import (
    setup_graveyard_tab, setup_audit_log_tab,
    write_to_graveyard, append_audit_log, queue_substitute,
)

# ── Protected statuses — NEVER touch these ────────────────────────────────────
PROTECTED = {"ACTIVE", "READY", "APPROVED", "LISTED", "AUDIT_REVIEW"}


def _col_to_idx(letter: str) -> int:
    result = 0
    for c in letter.upper():
        result = result * 26 + (ord(c) - ord("A") + 1)
    return result - 1


def _safe(row, idx, default=""):
    try:
        return str(row[idx]).strip() if idx < len(row) else default
    except Exception:
        return default


def _safe_float(val, default=0.0):
    """Parse float from cell value. Handles blank, None, #VALUE!, #REF!."""
    if val is None:
        return default
    s = str(val).strip()
    if not s or s.startswith("#") or s == "N/A":
        return default
    try:
        return float(s.replace("$", "").replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return default


def _days_since(timestamp_str: str) -> int:
    """Days since 'YYYY-MM-DD HH:MM'. Returns 999 if unparseable."""
    if not timestamp_str:
        return 999
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(timestamp_str.strip()[:16], fmt)
            return (datetime.now() - dt).days
        except ValueError:
            continue
    return 999


def _send_telegram(text: str) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("Telegram audit notification skipped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        return
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req     = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram audit notification sent.")
    except Exception as e:
        logger.warning(f"Telegram notification failed (non-fatal): {e}")


def run_audit(config, COL, service, sheet_name, start_row, end_row):
    """
    CFO audit pass. Reads all rows, applies auto-remove rules, flags gray areas,
    archives removed rows to Graveyard, logs to Audit Log, notifies via Telegram.
    """
    run_date = datetime.now().strftime("%Y-%m-%d")
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info(f"Audit started: {run_time}")

    # Ensure tabs exist
    setup_graveyard_tab(service)
    setup_audit_log_tab(service)

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")
    logger.info(f"Audit: read {len(all_data)} rows from sheet.")

    to_remove     = []   # (sheet_row, product_dict, reason_str)
    to_flag       = []   # (sheet_row, product_dict, reason_str)
    rows_reviewed = 0

    for idx, row in enumerate(all_data):
        if not row or not row[0]:
            continue

        sheet_row    = idx + start_row
        status       = _safe(row, _col_to_idx(COL["status"]))
        title        = _safe(row, _col_to_idx(COL["title"]))
        category     = _safe(row, _col_to_idx(COL["category"]))
        net_profit   = _safe_float(_safe(row, _col_to_idx(COL["net_profit"])))
        sold_90d_raw = _safe(row, _col_to_idx(COL["sold_90d"]))
        score_raw    = _safe(row, _col_to_idx(COL["demand_score"]))
        last_checked = _safe(row, _col_to_idx(COL["last_checked"]))
        costco_cost  = _safe_float(_safe(row, _col_to_idx(COL["costco_cost"])))
        ebay_price   = _safe_float(_safe(row, _col_to_idx(COL["ebay_price"])))
        full_notes   = _safe(row, 47)  # AV index

        if not status or not title:
            continue
        if status in PROTECTED:
            continue

        rows_reviewed += 1

        try:
            sold_90d = int(float(sold_90d_raw)) if sold_90d_raw else 0
        except (ValueError, TypeError):
            sold_90d = 0

        try:
            score = float(score_raw) if score_raw else None
        except (ValueError, TypeError):
            score = None

        days_since_checked = _days_since(last_checked)

        product_dict = {
            "title":            title,
            "category":         category or "Unknown",
            "status":           status,
            "net_profit":       net_profit,
            "sold_90d":         sold_90d,
            "score":            score,
            "costco_cost":      costco_cost,
            "ebay_price":       ebay_price,
            "days_on_sheet":    days_since_checked,
            "original_row":     sheet_row,
        }

        # ── Auto-remove rules ─────────────────────────────────────────────────
        remove_reason = None

        if "wrong_product_flag" in full_notes.lower():
            remove_reason = "Wrong product flag — comps matched wrong item"
        elif net_profit < 0:
            remove_reason = f"Negative net profit (${net_profit:.2f})"
        elif net_profit < 0.50:
            remove_reason = f"Below floor ($0.50 min) — net ${net_profit:.2f}"
        elif sold_90d == 0 and net_profit < 5.0:
            remove_reason = f"Zero velocity + net < $5 (net ${net_profit:.2f})"
        elif status == "PAUSED_OOS" and days_since_checked >= 45:
            remove_reason = f"OOS {days_since_checked}+ days, no restock"
        elif status in ("PENDING", "WATCH") and days_since_checked >= 60:
            remove_reason = f"Stale {days_since_checked} days (no progress)"

        if remove_reason:
            logger.info(f"  Row {sheet_row} AUTO-REMOVE [{remove_reason}]: {title[:50]}")
            to_remove.append((sheet_row, product_dict, remove_reason))
            continue

        # ── AUDIT_REVIEW rules ────────────────────────────────────────────────
        flag_reason = None

        if 0.50 <= net_profit < 1.00:
            flag_reason = f"Borderline net (${net_profit:.2f}) — below $1 floor"
        elif status in ("PENDING", "WATCH") and 45 <= days_since_checked < 60:
            flag_reason = f"Stale {days_since_checked} days — might be salvageable"
        elif sold_90d == 0 and net_profit >= 5.0:
            flag_reason = f"Zero velocity but net ${net_profit:.2f} — no proven demand, high upside"

        if flag_reason:
            logger.info(f"  Row {sheet_row} AUDIT_REVIEW [{flag_reason}]: {title[:50]}")
            to_flag.append((sheet_row, product_dict, flag_reason))

    logger.info(f"Decisions: {len(to_remove)} remove | {len(to_flag)} flag | {rows_reviewed} reviewed")

    # ── Apply AUDIT_REVIEW flags BEFORE deleting rows ────────────────────────
    for sheet_row, product_dict, reason in to_flag:
        write_row_partial(service, sheet_name, sheet_row, [
            (COL["status"],      "AUDIT_REVIEW"),
            (COL["tier_summary"], f"[AUDIT_REVIEW] {reason}"),
        ])

    # ── Write to Graveyard BEFORE deleting ───────────────────────────────────
    graveyard_rows = []
    sub_categories = []

    for sheet_row, product_dict, reason in to_remove:
        is_economics = any(k in reason for k in ["Negative", "Below floor", "Zero velocity"])
        graveyard_rows.append({
            "date_removed":      run_date,
            "reason":            reason,
            "category":          product_dict["category"],
            "title":             product_dict["title"],
            "cost":              product_dict["costco_cost"],
            "ebay_price":        product_dict["ebay_price"],
            "net_profit":        product_dict["net_profit"],
            "sold_90d":          product_dict["sold_90d"],
            "score":             product_dict["score"] or "",
            "status_at_removal": product_dict["status"],
            "days_on_sheet":     product_dict["days_on_sheet"],
            "substitute_queued": "YES" if is_economics else "NO",
            "original_row":      product_dict["original_row"],
        })
        if is_economics:
            sub_categories.append(product_dict["category"])

    if graveyard_rows:
        write_to_graveyard(service, graveyard_rows)

    # ── Delete rows in REVERSE order (highest row number first) ──────────────
    if to_remove:
        spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
        try:
            meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
            sheet_gid = next(
                s["properties"]["sheetId"]
                for s in meta["sheets"]
                if s["properties"]["title"] == sheet_name
            )
        except Exception as e:
            logger.error(f"Could not get sheet GID for deletion: {e}")
            sheet_gid = 0

        sorted_removes = sorted(to_remove, key=lambda x: x[0], reverse=True)
        for sheet_row, product_dict, reason in sorted_removes:
            try:
                body = {"requests": [{"deleteDimension": {"range": {
                    "sheetId":    sheet_gid,
                    "dimension":  "ROWS",
                    "startIndex": sheet_row - 1,  # 0-based
                    "endIndex":   sheet_row,
                }}}]}
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id, body=body
                ).execute()
                logger.info(f"  Deleted row {sheet_row}: {product_dict['title'][:40]}")
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"  Failed to delete row {sheet_row}: {e}")

    # ── Queue substitute PENDING rows ─────────────────────────────────────────
    n_subs = 0
    for cat in sub_categories:
        try:
            queue_substitute(service, sheet_name, cat)
            n_subs += 1
        except Exception as e:
            logger.warning(f"  Substitute queue failed for {cat}: {e}")

    # ── Category health scores ────────────────────────────────────────────────
    updated_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")
    cat_buckets  = defaultdict(lambda: {"nets": [], "velocities": [], "total": 0})

    for row in updated_data:
        if not row or not row[0]:
            continue
        status = _safe(row, _col_to_idx(COL["status"]))
        if status in PROTECTED or not status:
            continue
        cat = _safe(row, _col_to_idx(COL["category"])) or "Unknown"
        net = _safe_float(_safe(row, _col_to_idx(COL["net_profit"])))
        vel_raw = _safe(row, _col_to_idx(COL["sold_90d"]))
        try:
            vel = int(float(vel_raw)) if vel_raw else 0
        except (ValueError, TypeError):
            vel = 0
        cat_buckets[cat]["nets"].append(net)
        cat_buckets[cat]["velocities"].append(vel)
        cat_buckets[cat]["total"] += 1

    category_health = {}
    for cat, data in cat_buckets.items():
        nets  = data["nets"]
        vels  = data["velocities"]
        total = data["total"]
        if total == 0:
            category_health[cat] = 0
            continue
        viable_count = sum(1 for n, v in zip(nets, vels) if n >= 1.0 and v > 0)
        viable_pct   = viable_count / total
        avg_net      = sum(nets) / len(nets) if nets else 0
        avg_vel      = sum(vels) / len(vels) if vels else 0
        net_score    = min(avg_net / 10.0, 1.0)
        vel_score    = min(avg_vel / 30.0, 1.0)
        category_health[cat] = int((viable_pct * 50) + (net_score * 25) + (vel_score * 25))

    # ── Audit Log ─────────────────────────────────────────────────────────────
    audit_summary = {
        "date":               run_date,
        "mode":               "audit",
        "rows_reviewed":      rows_reviewed,
        "auto_removed":       len(to_remove),
        "flagged_review":     len(to_flag),
        "substitutes_queued": n_subs,
        "category_health":    json.dumps(category_health),
        "notes":              (f"Removed: {[r[2][:25] for r in to_remove[:3]]}" if to_remove else "Clean pass"),
    }
    append_audit_log(service, audit_summary)

    # ── Telegram ──────────────────────────────────────────────────────────────
    health_lines = "\n".join(
        f"• {cat}: {score}/100"
        for cat, score in sorted(category_health.items(), key=lambda x: -x[1])
    )
    tg_text = (
        f"🧹 <b>Audit Complete — {run_date}</b>\n"
        f"Auto-removed: {len(to_remove)} rows\n"
        f"Flagged for review: {len(to_flag)} rows\n"
        f"Substitutes queued: {n_subs}\n"
        f"\n<b>Category Health:</b>\n{health_lines or '(no data)'}\n"
    )
    if to_flag:
        tg_text += "\n⚠️ Filter col A = AUDIT_REVIEW to see rows needing your decision."
    _send_telegram(tg_text)

    logger.info(f"Audit complete. Removed: {len(to_remove)} | Flagged: {len(to_flag)}")
