"""
One-shot cleanup: remove rows added by the discovery run that shouldn't be there.
Identifies rows where Col T notes = "Discovered by agent — awaiting research"
and deletes them in a single batch. All other rows are untouched.

Run: python tools/cleanup_discoveries.py
     python tools/cleanup_discoveries.py --dry-run   (preview only, no deletes)
"""

import os
import sys
import argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

from loguru import logger
from tools.sheet_writer import get_sheets_service, read_sheet

DISCOVERY_NOTE = "Discovered by agent — awaiting research"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print rows that would be deleted without deleting them")
    args = parser.parse_args()

    import yaml
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "categories.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)
    business   = config["business"]
    sheet_name = business["sheet_name"]
    start_row  = business["data_start_row"]
    end_row    = business["data_end_row"]

    service        = get_sheets_service()
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")

    rows = read_sheet(service, f"'{sheet_name}'!A{start_row}:T{end_row}")

    # Find rows to delete — match on Col T (index 19) notes text
    rows_to_delete = []
    for i, row in enumerate(rows):
        notes = row[19].strip() if len(row) > 19 else ""
        title = row[2].strip() if len(row) > 2 else ""
        if DISCOVERY_NOTE in notes:
            sheet_row = start_row + i
            rows_to_delete.append((sheet_row, title))

    if not rows_to_delete:
        logger.info("No discovery-only rows found — sheet is already clean.")
        return

    logger.info(f"Found {len(rows_to_delete)} discovery-only rows to remove:")
    for r, t in rows_to_delete:
        logger.info(f"  Row {r}: {t[:60]}")

    if args.dry_run:
        logger.info("Dry-run mode — no changes made.")
        return

    # Get the sheet ID (tab ID, not spreadsheet ID)
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_id = None
    for s in meta["sheets"]:
        if s["properties"]["title"] == sheet_name:
            sheet_id = s["properties"]["sheetId"]
            break

    if sheet_id is None:
        logger.error(f"Tab '{sheet_name}' not found in spreadsheet.")
        return

    # Build delete requests — must delete from bottom to top so row indices stay valid
    requests = []
    for sheet_row, _ in sorted(rows_to_delete, reverse=True):
        row_idx = sheet_row - 1  # 0-based
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": row_idx,
                    "endIndex": row_idx + 1,
                }
            }
        })

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()

    logger.info(f"Deleted {len(rows_to_delete)} rows successfully.")
    logger.info("Run 'python agents/scheduler.py --mode research' to re-score existing products only.")


if __name__ == "__main__":
    main()
