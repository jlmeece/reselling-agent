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


def _col_idx(col: str) -> int:
    """Column letter(s) → 0-based index. 'A'=0, 'Z'=25, 'AA'=26, 'AI'=34, etc."""
    col, idx = col.upper(), 0
    for c in col:
        idx = idx * 26 + (ord(c) - ord('A') + 1)
    return idx - 1


def _write_formula_columns(service, spreadsheet_id, tab_id, data_start_row, num_rows=300):
    """
    Seeds formula columns I, J, N, Z, AC, AF, AG, AH for every data row.

    Uses spreadsheets().batchUpdate with userEnteredValue.formulaValue — this writes
    a true formula regardless of cell format. values().batchUpdate with USER_ENTERED
    silently stores formulas as text when a cell or column is in 'Plain text' format.
    Always overwrites: formulas are idempotent and re-running is safe.
    """
    # (formula_fn, number_format) — format clears Plain-text so formulaValue evaluates
    _PCT  = {"type": "PERCENT", "pattern": "0%"}
    _USD  = {"type": "NUMBER",  "pattern": "$#,##0.00"}
    _NUM  = {"type": "NUMBER",  "pattern": "0.0"}

    formulas = {
        "I":  (lambda r: f'=IF(H{r}<>"",H{r}-G{r}-AC{r}-AD{r}-AE{r},"")', _USD),
        "J":  (lambda r: f'=IF(H{r}>0,I{r}/H{r},"")',                       _PCT),
        "N":  (lambda r: f'=IFERROR(M{r}/MAX(K{r},1),"")',                   _NUM),
        "Z":  (lambda r: f'=IFERROR(G{r}+AD{r},G{r})',                       _USD),
        "AC": (lambda r: f'=IF(H{r}<>"",H{r}*AB{r},"")',                     _USD),
        "AF": (lambda r: f'=IF(G{r}<>"",G{r}*0.0825,"")',                    _USD),
        "AG": (lambda r: f'=IF(H{r}<>"",H{r}*0.90-G{r},"")',                 _USD),
        "AH": (lambda r: f'=IF(I{r}<>"",I{r}*0.15,"")',                      _USD),
    }

    requests = []
    for col_letter, (fn, fmt) in formulas.items():
        col_index = _col_idx(col_letter)
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId":          tab_id,
                    "startRowIndex":    data_start_row - 1,           # 0-based, inclusive
                    "endRowIndex":      data_start_row - 1 + num_rows, # 0-based, exclusive
                    "startColumnIndex": col_index,
                    "endColumnIndex":   col_index + 1,
                },
                "rows": [
                    {"values": [{
                        "userEnteredValue": {"formulaValue": fn(row)},
                        "userEnteredFormat": {"numberFormat": fmt},
                    }]}
                    for row in range(data_start_row, data_start_row + num_rows)
                ],
                "fields": "userEnteredValue,userEnteredFormat.numberFormat",
            }
        })

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()

    logger.info(f"Formula columns seeded: {', '.join(formulas.keys())} ({num_rows} rows each).")


def _delete_legend_tab(service, spreadsheet_id):
    """Remove the stale Legend tab if it exists."""
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for sheet in meta["sheets"]:
            if sheet["properties"]["title"] == "Legend":
                legend_id = sheet["properties"]["sheetId"]
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": [{"deleteSheet": {"sheetId": legend_id}}]},
                ).execute()
                logger.info("Removed stale Legend tab.")
                return
    except Exception as e:
        logger.warning(f"Could not check/remove Legend tab: {e}")


def _write_legend_tab(service, spreadsheet_id):
    """
    Creates (or overwrites) a 'Legend' tab explaining every status value,
    column formula, and scoring threshold used by the agent.
    Safe to re-run — always overwrites.
    """
    # Ensure the Legend tab exists
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_titles = {s["properties"]["title"] for s in meta["sheets"]}
    if "Legend" not in existing_titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": "Legend"}}}]},
        ).execute()
        logger.info("Created 'Legend' tab.")
    else:
        logger.info("'Legend' tab already exists — overwriting.")

    rows = [
        ["WAT Reselling Agent — Legend", "", ""],
        ["", "", ""],
        ["STATUS VALUES", "Meaning", "Your next action"],
        ["PENDING",         "Discovered, not yet researched",                        "Wait — agent will research on next run"],
        ["SCORED",          "Tier 1 result — scored ≥ 6.0, ready for your review",   "Review col AV notes → change to APPROVED or PAUSED_*"],
        ["WATCH",           "Tier 2 result — scored ≥ 3.0, monitor pricing",          "Check back in 2–4 weeks; reprice and approve if margin improves"],
        ["APPROVED",        "You've approved it — ready for listing copy",             "Wait for READY status, or run ebay_export.py manually"],
        ["READY",           "Listing copy generated, ready to export to eBay",         "Run: python tools/ebay_export.py"],
        ["LISTED",          "Exported to eBay, awaiting first sale",                   "Check eBay Seller Hub; update col Q with listing URL"],
        ["ACTIVE",          "Live eBay listing, actively monitored",                   "No action — agent monitors price, stock, and sale expiry"],
        ["PAUSED_OOS",      "Paused — product went out of stock at Costco",            "Agent will re-check; restore to ACTIVE when back in stock"],
        ["PAUSED_MARGIN",   "Paused — margin dropped below threshold",                 "Review pricing; manually change to ACTIVE if margin recovers"],
        ["PAUSED_DEMAND",   "Paused — Tier 3, low eBay demand",                       "Check re_eval_date col S — agent will re-score then"],
        ["PAUSED_SEASONAL", "Paused — seasonal product, wrong time of year",           "Check col S for re-eval date; approve manually when season returns"],
        ["", "", ""],
        ["SCORING THRESHOLDS", "Score range", "Action"],
        ["Tier 1",  "≥ 6.0",           "Buy and list — strong demand, positive margin"],
        ["Tier 2",  "3.0 – 5.9",       "Watch — borderline; monitor for price/stock changes"],
        ["Tier 3",  "< 3.0",           "Skip — low demand or margin too thin"],
        ["", "", ""],
        ["FORMULA COLUMNS (do not overwrite)", "Formula", "What it shows"],
        ["I  (net_profit)",    "=H-G-AC-AD-AE",        "eBay price minus all costs"],
        ["J  (net_margin)",    "=IF(H>0,I/H,0)",        "Net profit as % of sale price"],
        ["N  (comp_sat)",      "=IFERROR(M/MAX(K,1),\"\")","Active listings ÷ 90-day sold"],
        ["Z  (total_cost)",    "=IFERROR(G+AD,G)",      "Costco cost + shipping"],
        ["AC (ebay_fees)",     "=H*AB",                  "eBay fees (fee_rate × price)"],
        ["AF (tax_est)",       "=G*0.0825",              "Estimated Costco tax"],
        ["AG (site_profit)",   "=H*0.90-G",             "Gross margin at 90% of price"],
        ["AH (ad_budget)",     "=I*0.15",               "15% of net profit for promoted listings"],
        ["", "", ""],
        ["KEY COLUMNS", "Column", "Notes"],
        ["AB  fee_rate",       "AB", "eBay fee rate — default 0.1325 (13.25%)"],
        ["AD  ship_cost",      "AD", "Costco shipping cost (0 if free shipping)"],
        ["AE  fulfillment",    "AE", "Fulfillment cost (manual entry, usually 0 for dropship)"],
        ["AV  full_notes",     "AV", "Full research narrative — hidden, view in formula bar"],
    ]

    data = [{"range": "Legend!A1", "values": rows}]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    logger.success("Legend tab written.")


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

    # ── Seed formula columns (I, J, N, Z, AC, AF, AG, AH) ────────────────────
    # These are never written by the agent — they must exist as Sheet formulas.
    # Re-running this is safe: only blank cells are touched.
    _write_formula_columns(service, spreadsheet_id, tab_id, DATA_START_ROW)

    _delete_legend_tab(service, spreadsheet_id)
    logger.success("Dashboard is ready. Open Google Sheets to review.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Set up the reselling agent Google Sheet dashboard.")
    parser.add_argument("--legend-only", action="store_true", help="Only write/update the Legend tab, skip full dashboard setup.")
    args = parser.parse_args()

    if args.legend_only:
        spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
        if not spreadsheet_id:
            logger.error("GOOGLE_SHEET_ID not set in .env")
            sys.exit(1)
        service = get_sheets_service()
        _delete_legend_tab(service, spreadsheet_id)
    else:
        main()
