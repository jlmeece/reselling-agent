"""
Graveyard Writer
================
Manages the Graveyard and Audit Log tabs in Google Sheets.
Both tabs are append-only permanent records — never modify existing rows.
"""

import os
import sys
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.sheet_writer import get_sheets_service, execute_with_retry


SHEET_ID = None  # resolved lazily from env

# Single source of truth for the Graveyard tab's header row. Columns N:Q are the
# re-check extensions (2026-10-08): COSTCO_URL + SKU captured at removal time, and
# LAST_CHECKED + VERDICT written later by the graveyard sweep (--mode graveyard-sweep).
GRAVEYARD_HEADERS = [
    "DATE_REMOVED", "REASON", "CATEGORY", "TITLE",
    "COST", "EBAY_PRICE", "NET_PROFIT", "SOLD_90D",
    "SCORE", "STATUS_AT_REMOVAL", "DAYS_ON_SHEET",
    "SUBSTITUTE_QUEUED", "ORIGINAL_ROW",
    "COSTCO_URL", "SKU", "LAST_CHECKED", "VERDICT",
]


def _get_sheet_id():
    sid = os.getenv("GOOGLE_SHEET_ID")
    if not sid:
        raise RuntimeError("GOOGLE_SHEET_ID not set in environment.")
    return sid


def _get_tab_names(service) -> set:
    """Return set of existing tab names."""
    meta = service.spreadsheets().get(spreadsheetId=_get_sheet_id()).execute()
    return {s["properties"]["title"] for s in meta["sheets"]}


def setup_graveyard_tab(service) -> None:
    """Create the Graveyard tab if missing, and (re)write the header row. Idempotent.

    Always (re)writing the header row is what migrates a pre-2026-10-08 tab to the
    extended N:Q columns — it overwrites only row 1, never the data rows below.
    """
    if "Graveyard" not in _get_tab_names(service):
        body = {"requests": [{"addSheet": {"properties": {"title": "Graveyard"}}}]}
        execute_with_retry(
            service.spreadsheets().batchUpdate(spreadsheetId=_get_sheet_id(), body=body),
            "graveyard addSheet", retry_statuses=(429,), retry_timeouts=False,
        )
    execute_with_retry(service.spreadsheets().values().update(
        spreadsheetId=_get_sheet_id(),
        range="Graveyard!A1",
        valueInputOption="RAW",
        body={"values": [GRAVEYARD_HEADERS]},
    ), "graveyard headers")
    logger.info("Graveyard tab ready (headers refreshed).")


def setup_audit_log_tab(service) -> None:
    """Create Audit Log tab if it doesn't exist. Idempotent."""
    if "Audit Log" in _get_tab_names(service):
        return
    header = [[
        "DATE", "MODE", "ROWS_REVIEWED", "AUTO_REMOVED",
        "FLAGGED_REVIEW", "SUBSTITUTES_QUEUED", "CATEGORY_HEALTH", "NOTES",
    ]]
    body = {"requests": [{"addSheet": {"properties": {"title": "Audit Log"}}}]}
    execute_with_retry(
        service.spreadsheets().batchUpdate(spreadsheetId=_get_sheet_id(), body=body),
        "audit log addSheet", retry_statuses=(429,), retry_timeouts=False,
    )
    execute_with_retry(service.spreadsheets().values().update(
        spreadsheetId=_get_sheet_id(),
        range="Audit Log!A1",
        valueInputOption="RAW",
        body={"values": header},
    ), "audit log headers")
    logger.info("Audit Log tab created.")


def get_graveyard_titles(service) -> set:
    """Return lowercased set of product titles already in Graveyard. Used to block re-adding losers."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=_get_sheet_id(),
            range="Graveyard!D2:D500",
        ).execute()
        rows = result.get("values", [])
        return {row[0].strip().lower() for row in rows if row}
    except Exception:
        return set()


# Column positions in GRAVEYARD_HEADERS (0-based) — used by the graveyard sweep.
GY_DATE_REMOVED, GY_REASON, GY_CATEGORY, GY_TITLE, GY_COST, GY_EBAY_PRICE, GY_NET, GY_SOLD = range(8)
GY_SCORE, GY_STATUS, GY_DAYS, GY_SUB, GY_ORIGINAL_ROW = range(8, 13)
GY_COSTCO_URL, GY_SKU, GY_LAST_CHECKED, GY_VERDICT = 13, 14, 15, 16


def read_graveyard(service, max_rows=5000) -> list[dict]:
    """Read Graveyard rows (A2:Q{max_rows}) into dicts with the absolute `row_num`.
    Never raises — returns [] on a missing tab / read error."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=_get_sheet_id(), range=f"Graveyard!A2:Q{max_rows + 1}"
        ).execute()
    except Exception as e:
        logger.warning(f"Graveyard read failed: {e}")
        return []

    def cell(r, j):
        return str(r[j]).strip() if j < len(r) else ""

    out = []
    for i, r in enumerate(result.get("values", [])):
        if not r or not any(str(x).strip() for x in r):
            continue
        out.append({
            "row_num": 2 + i,
            "date_removed": cell(r, GY_DATE_REMOVED), "reason": cell(r, GY_REASON),
            "category": cell(r, GY_CATEGORY), "title": cell(r, GY_TITLE),
            "cost": cell(r, GY_COST), "ebay_price": cell(r, GY_EBAY_PRICE),
            "net_profit": cell(r, GY_NET), "sold_90d": cell(r, GY_SOLD),
            "score": cell(r, GY_SCORE), "status": cell(r, GY_STATUS),
            "days_on_sheet": cell(r, GY_DAYS), "substitute_queued": cell(r, GY_SUB),
            "original_row": cell(r, GY_ORIGINAL_ROW), "costco_url": cell(r, GY_COSTCO_URL),
            "sku": cell(r, GY_SKU), "last_checked": cell(r, GY_LAST_CHECKED),
            "verdict": cell(r, GY_VERDICT),
        })
    return out


def write_graveyard_verdict(service, row_num, last_checked, verdict) -> bool:
    """Write LAST_CHECKED (P) + VERDICT (Q) for one Graveyard row. Never raises."""
    try:
        execute_with_retry(service.spreadsheets().values().update(
            spreadsheetId=_get_sheet_id(),
            range=f"Graveyard!P{row_num}:Q{row_num}",
            valueInputOption="RAW",
            body={"values": [[last_checked, verdict]]},
        ), "graveyard verdict")
        return True
    except Exception as e:
        logger.warning(f"Graveyard verdict write failed (row {row_num}): {e}")
        return False


def write_to_graveyard(service, removed_rows: list) -> None:
    """Append rows to Graveyard tab. Never overwrites existing rows."""
    if not removed_rows:
        return
    values = []
    for r in removed_rows:
        values.append([
            str(r.get("date_removed", "")),
            str(r.get("reason", "")),
            str(r.get("category", "")),
            str(r.get("title", "")),
            str(r.get("cost", "")),
            str(r.get("ebay_price", "")),
            str(r.get("net_profit", "")),
            str(r.get("sold_90d", "")),
            str(r.get("score", "")),
            str(r.get("status_at_removal", "")),
            str(r.get("days_on_sheet", "")),
            str(r.get("substitute_queued", "NO")),
            str(r.get("original_row", "")),
            str(r.get("costco_url", "")),      # N — for the graveyard sweep re-check
            str(r.get("sku", "")),             # O — stable product handle
        ])
    execute_with_retry(service.spreadsheets().values().append(
        spreadsheetId=_get_sheet_id(),
        range="Graveyard!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ), "graveyard append")
    logger.info(f"Graveyard: appended {len(values)} rows.")


def append_audit_log(service, summary: dict) -> None:
    """Append one row to Audit Log tab."""
    row = [[
        str(summary.get("date", "")),
        str(summary.get("mode", "")),
        str(summary.get("rows_reviewed", "")),
        str(summary.get("auto_removed", "")),
        str(summary.get("flagged_review", "")),
        str(summary.get("substitutes_queued", "")),
        str(summary.get("category_health", "")),
        str(summary.get("notes", "")),
    ]]
    execute_with_retry(service.spreadsheets().values().append(
        spreadsheetId=_get_sheet_id(),
        range="Audit Log!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": row},
    ), "audit log append")
    logger.info("Audit Log: entry appended.")


def queue_substitute(service, sheet_name: str, category: str) -> None:
    """
    Previously wrote a blank PENDING placeholder row — removed because those rows
    have no URL and are skipped by research, polluting the sheet.
    Now just logs that a discovery run is needed for this category.
    """
    logger.info(f"Category needs discovery run to refill: {category} (no placeholder row written)")
