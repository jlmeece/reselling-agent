import sys, os
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)
from tools.sheet_writer import get_sheets_service

service = get_sheets_service()
sheet_id = os.getenv("GOOGLE_SHEET_ID")
meta = service.spreadsheets().get(spreadsheetId=sheet_id, includeGridData=False).execute()

for sheet in meta["sheets"]:
    if sheet["properties"]["title"] == "Product Tracker":
        merges = sheet.get("merges", [])
        print(f"Merged ranges in Product Tracker: {len(merges)}")
        for m in merges:
            print(f"  rows {m['startRowIndex']}-{m['endRowIndex']} "
                  f"cols {m['startColumnIndex']}-{m['endColumnIndex']}")
        frozen = sheet["properties"].get("gridProperties", {})
        print(f"Current freeze: rows={frozen.get('frozenRowCount',0)} cols={frozen.get('frozenColumnCount',0)}")
