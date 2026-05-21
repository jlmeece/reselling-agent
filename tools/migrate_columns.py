"""
One-time migration: remaps Product Tracker data from old column layout to new.

Old layout (A–AJ, 36 cols):
  A=title, B=sku, C=category, D=costco_url, E=costco_cost, F=ebay_price,
  G=fee_rate, H=ebay_fees(f), I=ship_cost, J=net_profit(f), K=net_margin(f),
  L=seo_title, M=bullets, N=description, O=redirect_msg, P=meta_desc,
  Q=keywords, R=alt_text, S=sold_90d, T=avg_price, U=comp_count,
  V=demand_score, W=last_checked, X=fulfillment, Y=stock_status,
  Z=tax_est(f), AA=site_profit(f), AB=ad_budget(f), AC=google_hl,
  AD=google_desc, AE=meta_text, AF=meta_hl, AG=status, AH=image_urls,
  AI=price_change, AJ=notes

New layout (A–AM, 39 cols):
  A=status, B=demand_score, C=title, D=category, E=platform(NEW),
  F=stock_status, G=costco_cost, H=ebay_price, I=net_profit(f),
  J=net_margin(f), K=sold_90d, L=avg_price, M=comp_count, N=last_checked,
  O=price_change, P=ebay_listing_url(NEW), Q=costco_url, R=re_eval_date(NEW),
  S=notes, T=sku, U=fee_rate, V=ebay_fees(f), W=ship_cost, X=fulfillment,
  Y=tax_est(f), Z=site_profit(f), AA=ad_budget(f), AB=seo_title,
  AC=bullets, AD=description, AE=redirect_msg, AF=meta_desc, AG=keywords,
  AH=alt_text, AI=google_hl, AJ=google_desc, AK=meta_text, AL=meta_hl,
  AM=image_urls

Formula columns are left blank — setup_sheet.py writes them.
Run once: python tools/migrate_columns.py
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)
from tools.sheet_writer import get_sheets_service

# Old index → new index mapping (0-based). Formula cols mapped to -1 = skip.
# fmt: off
OLD_TO_NEW = {
    0:  2,   # title        → C
    1:  19,  # sku          → T
    2:  3,   # category     → D
    3:  16,  # costco_url   → Q
    4:  6,   # costco_cost  → G
    5:  7,   # ebay_price   → H
    6:  20,  # fee_rate     → U
    7:  -1,  # ebay_fees    — formula, skip
    8:  22,  # ship_cost    → W
    9:  -1,  # net_profit   — formula, skip
    10: -1,  # net_margin   — formula, skip
    11: 27,  # seo_title    → AB
    12: 28,  # bullets      → AC
    13: 29,  # description  → AD
    14: 30,  # redirect_msg → AE
    15: 31,  # meta_desc    → AF
    16: 32,  # keywords     → AG
    17: 33,  # alt_text     → AH
    18: 10,  # sold_90d     → K
    19: 11,  # avg_price    → L
    20: 12,  # comp_count   → M
    21: 1,   # demand_score → B
    22: 13,  # last_checked → N
    23: 23,  # fulfillment  → X
    24: 5,   # stock_status → F
    25: -1,  # tax_est      — formula, skip
    26: -1,  # site_profit  — formula, skip
    27: -1,  # ad_budget    — formula, skip
    28: 34,  # google_hl    → AI
    29: 35,  # google_desc  → AJ
    30: 36,  # meta_text    → AK
    31: 37,  # meta_hl      → AL
    32: 0,   # status       → A
    33: 38,  # image_urls   → AM
    34: 14,  # price_change → O
    35: 18,  # notes        → S
}
# fmt: on

NEW_COL_COUNT  = 39   # A–AM
DATA_START     = 4    # first data row (1-indexed)
DATA_END       = 200
VALID_STATUSES = {"PENDING", "APPROVED", "ACTIVE", "WATCH", "PAUSED", "URGENT", ""}


def col_letter(idx):
    """0-based index → column letter(s). 0=A, 25=Z, 26=AA, etc."""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def migrate(dry_run=False):
    service = get_sheets_service()
    sheet_id   = os.getenv("GOOGLE_SHEET_ID")
    sheet_name = "Product Tracker"

    # Read all existing data
    read_range = f"'{sheet_name}'!A{DATA_START}:{col_letter(35)}{DATA_END}"
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=read_range
    ).execute()
    old_rows = result.get("values", [])
    print(f"Read {len(old_rows)} rows from old layout.")

    # Build new rows
    junk_count = 0
    new_rows = []
    for old_row in old_rows:
        # Skip completely empty rows
        if not any(cell.strip() for cell in old_row if cell):
            new_rows.append([""] * NEW_COL_COUNT)
            continue
        # Skip header/junk rows: must have a valid status AND a Costco URL to be a real product
        old_status  = old_row[32] if len(old_row) > 32 else ""
        old_url     = old_row[3]  if len(old_row) > 3  else ""
        is_real_row = (old_status in VALID_STATUSES) and old_url.startswith("http")
        if not is_real_row:
            new_rows.append([""] * NEW_COL_COUNT)
            junk_count += 1
            continue
        new_row = [""] * NEW_COL_COUNT
        for old_idx, new_idx in OLD_TO_NEW.items():
            if new_idx == -1:
                continue
            val = old_row[old_idx] if old_idx < len(old_row) else ""
            new_row[new_idx] = val
        new_rows.append(new_row)

    if junk_count:
        print(f"Skipped {junk_count} junk/header rows (cleared to blank).")

    print(f"Built {len(new_rows)} new rows.")

    if dry_run:
        print("DRY RUN -- no writes. Rows with data:")
        for i, r in enumerate(new_rows):
            if any(c for c in r if c):
                status = r[0]
                tier   = r[1]
                title  = r[2][:45]
                cat    = r[3]
                stock  = r[5]
                cost   = r[6]
                notes  = r[18][:40]
                print(f"  Row {DATA_START+i}: status={status!r:10} tier={tier!r:5} "
                      f"cat={cat!r:20} stock={stock!r:12} cost={cost!r:8} title={title!r}")
                if notes:
                    print(f"           notes={notes!r}")
        return

    # Clear old data range (A4:AM200)
    clear_range = f"'{sheet_name}'!A{DATA_START}:{col_letter(NEW_COL_COUNT - 1)}{DATA_END}"
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=clear_range
    ).execute()
    print(f"Cleared {clear_range}")

    # Write new data
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!A{DATA_START}",
        valueInputOption="USER_ENTERED",
        body={"values": new_rows},
    ).execute()
    print(f"Written {len(new_rows)} rows to new layout. Migration complete.")


def migrate_add_total_cost_col(dry_run=False):
    """
    One-time migration: insert a new visible column Z (TOTAL COST) in the
    Product Tracker tab, shifting all former hidden cols (Z–AU) right by one.

    Safe to re-run — checks the Z header before inserting.
    Must run BEFORE col_map.yaml is updated (or the idempotency check still works
    because it reads the sheet directly, not col_map).
    """
    service    = get_sheets_service()
    sheet_id   = os.getenv("GOOGLE_SHEET_ID")
    sheet_name = "Product Tracker"

    # ── Idempotency check: read current col Z header (index 25) ──────────────
    check = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!Z3",   # row 3 is the column header row
    ).execute()
    current_z = (check.get("values") or [[""]])[0][0] if check.get("values") else ""
    if current_z == "TOTAL COST":
        print("Col Z already says 'TOTAL COST' — migration already applied. Skipping.")
        return

    print(f"Col Z currently shows: {current_z!r}. Proceeding with insert.")

    if dry_run:
        print("=== DRY RUN ===")
        print("DRY RUN — would insert dimension at col index 25 (Z), write header to Z3, and write IFERROR formulas.")
        return

    # ── Get tab sheetId ───────────────────────────────────────────────────────
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id, includeGridData=False
    ).execute()
    tab_id = None
    for s in meta["sheets"]:
        if s["properties"]["title"] == sheet_name:
            tab_id = s["properties"]["sheetId"]
            break
    if tab_id is None:
        raise ValueError(f"Tab '{sheet_name}' not found in spreadsheet.")

    # ── Insert one column at index 25 (zero-based = col Z) ───────────────────
    # Google Sheets auto-shifts all existing data and updates formula references.
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "insertDimension": {
                "range": {
                    "sheetId": tab_id,
                    "dimension": "COLUMNS",
                    "startIndex": 25,
                    "endIndex": 26,
                },
                "inheritFromBefore": False,
            }
        }]},
    ).execute()
    print("Column inserted at index 25 (col Z). Existing data shifted to AA+.")

    # Write the header so the idempotency check works on re-run
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!Z3",
        valueInputOption="RAW",
        body={"values": [["TOTAL COST"]]},
    ).execute()
    print("Header 'TOTAL COST' written to Z3.")

    # ── Write TOTAL COST formulas to Z4:Z1000 ────────────────────────────────
    # ship_cost is now in AD (was AC before insert). Formula: =IFERROR(G+AD, G)
    formula_rows = [["=IFERROR(G{r}+AD{r},G{r})".format(r=row)] for row in range(4, 1001)]
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!Z4",
        valueInputOption="USER_ENTERED",
        body={"values": formula_rows},
    ).execute()
    print("TOTAL COST formulas written to Z4:Z1000.")

    print("Migration complete. Run 'python agents/setup_sheet.py' to apply formatting.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="legacy",
                        choices=["legacy", "add-total-cost-col"],
                        help="Migration to run")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.command == "add-total-cost-col":
        migrate_add_total_cost_col(dry_run=args.dry_run)
    else:
        dry = args.dry_run
        if dry:
            print("=== DRY RUN ===")
        migrate(dry_run=dry)
