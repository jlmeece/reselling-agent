"""
agents/setup_sheet.py
One-time (re-runnable) script that turns the raw product sheet into a dashboard.

What it does:
  1. Detects whether the 3 header rows (title / stats / column headers) already exist
  2. If not, inserts 3 blank rows at the very top so existing data shifts to row 4+
  3. Calls setup_dashboard() to apply all formatting, conditional colors, frozen panes, etc.

Run any time the sheet looks wrong:
  python agents/setup_sheet.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

from loguru import logger
from tools.sheet_writer import get_sheets_service
from tools.sheet_formatter import setup_dashboard

SHEET_NAME    = "Product Tracker"
DATA_START_ROW = 4   # rows 1-3 = title / stats / headers


def _get_tab_id(service, spreadsheet_id, sheet_name):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == sheet_name:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Tab '{sheet_name}' not found in spreadsheet.")


def _insert_rows_at_top(service, spreadsheet_id, tab_id, count):
    """Insert `count` blank rows before row 1, pushing all existing data down."""
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [{
                "insertDimension": {
                    "range": {
                        "sheetId":    tab_id,
                        "dimension":  "ROWS",
                        "startIndex": 0,
                        "endIndex":   count,
                    },
                    "inheritFromBefore": False,
                }
            }]
        },
    ).execute()


def _insert_columns(service, spreadsheet_id, tab_id, start_index, count):
    """Insert `count` blank columns at start_index (0-based), shifting existing columns right."""
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [{
                "insertDimension": {
                    "range": {
                        "sheetId":    tab_id,
                        "dimension":  "COLUMNS",
                        "startIndex": start_index,
                        "endIndex":   start_index + count,
                    },
                    "inheritFromBefore": False,
                }
            }]
        },
    ).execute()


def main():
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not spreadsheet_id:
        logger.error("GOOGLE_SHEET_ID not set in .env")
        sys.exit(1)

    service = get_sheets_service()
    tab_id  = _get_tab_id(service, spreadsheet_id, SHEET_NAME)

    # ── Check whether header rows are already in place ────────────────────────
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME}'!A1",
    ).execute()
    a1_vals = result.get("values", [])
    a1_text = str(a1_vals[0][0]) if (a1_vals and a1_vals[0]) else ""

    if "Costco" in a1_text and "Dashboard" in a1_text:
        logger.info("Header rows already present — skipping row insert.")
    else:
        logger.info(f"Inserting {DATA_START_ROW - 1} header rows at top of sheet...")
        _insert_rows_at_top(service, spreadsheet_id, tab_id, DATA_START_ROW - 1)
        logger.info("  Existing data shifted to row 4+.")

    # ── Check whether SALE / SHIP COST / TOTAL COST columns (X, Y, Z) are inserted ──
    # Layout has 3 phases:
    #   Phase 1 (original): X3="SALE", Z3=""  (Z was first hidden col)
    #   Phase 2 (current):  X3="SALE", Z3="TOTAL COST"  (Z is now visible)
    # Both are valid post-insert states. Only insert if X is NOT "SALE".
    hr = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME}'!X3:Z3",
    ).execute()
    hr_row = (hr.get("values") or [[]])[0]
    x3_text = str(hr_row[0]).strip() if len(hr_row) > 0 else ""
    z3_text = str(hr_row[2]).strip() if len(hr_row) > 2 else ""

    # Properly inserted: X="SALE" and Z is either blank (old layout) or "TOTAL COST" (new layout)
    already_inserted = (x3_text.upper() == "SALE" and z3_text.upper() in ("", "TOTAL COST"))

    if already_inserted:
        logger.info("SALE / SHIP COST columns already properly inserted — skipping.")
    else:
        logger.info(
            f"Col X='{x3_text}', Z='{z3_text}' — columns not yet physically inserted. "
            "Inserting 2 columns at position 23 (after W) for SALE and SHIP COST..."
        )
        _insert_columns(service, spreadsheet_id, tab_id, start_index=23, count=2)
        logger.info("  Columns inserted. Old hidden cols shifted 2 right (old-X→Z, old-Y→AA, etc.).")
        logger.info("  Google Sheets auto-updated all formula references.")

    # ── Apply full dashboard formatting ───────────────────────────────────────
    setup_dashboard(service, SHEET_NAME, DATA_START_ROW)
    logger.success("Dashboard is ready. Open Google Sheets to review.")


if __name__ == "__main__":
    main()
