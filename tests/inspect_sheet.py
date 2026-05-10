"""Inspect current sheet structure — tabs and data rows."""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)
from tools.sheet_writer import get_sheets_service

service = get_sheets_service()
sheet_id = os.getenv("GOOGLE_SHEET_ID")

meta = service.spreadsheets().get(spreadsheetId=sheet_id, includeGridData=False).execute()

print("=== TABS ===")
for sheet in meta["sheets"]:
    p = sheet["properties"]
    hidden = p.get("hidden", False)
    rows = p.get("gridProperties", {}).get("rowCount", "?")
    cols = p.get("gridProperties", {}).get("columnCount", "?")
    merges = len(sheet.get("merges", []))
    print(f"  {'[HIDDEN]' if hidden else '        '} {p['title']} -- {rows} rows x {cols} cols, {merges} merges")

print()
print("=== PRODUCT TRACKER -- header rows ===")
result = service.spreadsheets().values().get(
    spreadsheetId=sheet_id, range="'Product Tracker'!A1:S3",
).execute()
for i, row in enumerate(result.get("values", []), 1):
    print(f"  Row {i}: {[str(c)[:25] for c in row]}")

print()
print("=== PRODUCT TRACKER -- data rows 4-15 (new layout: A=status, B=tier, C=title) ===")
result2 = service.spreadsheets().values().get(
    spreadsheetId=sheet_id, range="'Product Tracker'!A4:S15",
).execute()
for i, row in enumerate(result2.get("values", []), 4):
    if not row:
        continue
    status = row[0] if len(row) > 0 else "--"   # A
    tier   = row[1] if len(row) > 1 else "--"   # B
    title  = row[2][:40] if len(row) > 2 else "(empty)"  # C
    cat    = row[3] if len(row) > 3 else "--"   # D
    stock  = row[5] if len(row) > 5 else "--"   # F
    cost   = row[6] if len(row) > 6 else "--"   # G
    url    = row[17][:35] if len(row) > 17 else "--"  # R (costco_url)
    if any(c for c in row if c):
        print(f"  Row {i:3}: status={status:10} tier={str(tier):5} cat={cat:20} "
              f"stock={stock:12} cost={cost:8} title={title}")
