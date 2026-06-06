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

from tools.sheet_writer import get_sheets_service


SHEET_ID = None  # resolved lazily from env


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
    """Create Graveyard tab if it doesn't exist. Idempotent."""
    if "Graveyard" in _get_tab_names(service):
        return
    header = [[
        "DATE_REMOVED", "REASON", "CATEGORY", "TITLE",
        "COST", "EBAY_PRICE", "NET_PROFIT", "SOLD_90D",
        "SCORE", "STATUS_AT_REMOVAL", "DAYS_ON_SHEET",
        "SUBSTITUTE_QUEUED", "ORIGINAL_ROW",
    ]]
    body = {"requests": [{"addSheet": {"properties": {"title": "Graveyard"}}}]}
    service.spreadsheets().batchUpdate(spreadsheetId=_get_sheet_id(), body=body).execute()
    service.spreadsheets().values().update(
        spreadsheetId=_get_sheet_id(),
        range="Graveyard!A1",
        valueInputOption="RAW",
        body={"values": header},
    ).execute()
    logger.info("Graveyard tab created.")


def setup_audit_log_tab(service) -> None:
    """Create Audit Log tab if it doesn't exist. Idempotent."""
    if "Audit Log" in _get_tab_names(service):
        return
    header = [[
        "DATE", "MODE", "ROWS_REVIEWED", "AUTO_REMOVED",
        "FLAGGED_REVIEW", "SUBSTITUTES_QUEUED", "CATEGORY_HEALTH", "NOTES",
    ]]
    body = {"requests": [{"addSheet": {"properties": {"title": "Audit Log"}}}]}
    service.spreadsheets().batchUpdate(spreadsheetId=_get_sheet_id(), body=body).execute()
    service.spreadsheets().values().update(
        spreadsheetId=_get_sheet_id(),
        range="Audit Log!A1",
        valueInputOption="RAW",
        body={"values": header},
    ).execute()
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
        ])
    service.spreadsheets().values().append(
        spreadsheetId=_get_sheet_id(),
        range="Graveyard!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
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
    service.spreadsheets().values().append(
        spreadsheetId=_get_sheet_id(),
        range="Audit Log!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": row},
    ).execute()
    logger.info("Audit Log: entry appended.")


def queue_substitute(service, sheet_name: str, category: str) -> None:
    """
    Previously wrote a blank PENDING placeholder row — removed because those rows
    have no URL and are skipped by research, polluting the sheet.
    Now just logs that a discovery run is needed for this category.
    """
    logger.info(f"Category needs discovery run to refill: {category} (no placeholder row written)")
