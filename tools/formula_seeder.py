"""
Tool: formula_seeder
Seeds formula cells and static defaults for a single product row.
Called by researcher.py before writing product data so formulas exist
even for rows beyond the initial 300-row seed range from setup_sheet.py.
"""
import os
from loguru import logger


def _get_tab_id(service, spreadsheet_id, sheet_name):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == sheet_name:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Tab '{sheet_name}' not found in spreadsheet.")


def _col_idx(col: str) -> int:
    col, idx = col.upper(), 0
    for c in col:
        idx = idx * 26 + (ord(c) - ord('A') + 1)
    return idx - 1


def seed_formula_row(service, sheet_name: str, row_num: int, ad_rate: float = 0.0):
    """
    Writes formula cells and static defaults for row_num.
    Safe to call on every research pass — all writes are idempotent.
    Call this BEFORE write_row_partial so researcher.py's actual values win.
    """
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not spreadsheet_id:
        logger.warning("seed_formula_row: GOOGLE_SHEET_ID not set, skipping")
        return

    try:
        tab_id = _get_tab_id(service, spreadsheet_id, sheet_name)
    except Exception as e:
        logger.warning(f"seed_formula_row: could not get tab ID: {e}")
        return

    r = row_num
    formulas = {
        "I":  f"=H{r}-G{r}-AC{r}-AD{r}-AE{r}",
        "J":  f"=IF(H{r}>0,I{r}/H{r},0)",
        "Z":  f"=IFERROR(G{r}+AD{r},G{r})",
        "AC": f"=H{r}*AB{r}",
        "AE": f"=H{r}*{ad_rate}",
        "AF": f"=G{r}*0.0825",
        "AG": f"=H{r}*0.9-G{r}",
        "AH": f"=I{r}*0.15",
    }

    requests = []
    for col_letter, formula in formulas.items():
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId":          tab_id,
                    "startRowIndex":    r - 1,
                    "endRowIndex":      r,
                    "startColumnIndex": _col_idx(col_letter),
                    "endColumnIndex":   _col_idx(col_letter) + 1,
                },
                "rows": [{"values": [{"userEnteredValue": {"formulaValue": formula}}]}],
                "fields": "userEnteredValue",
            }
        })

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    except Exception as e:
        logger.warning(f"seed_formula_row: formula write failed for row {r}: {e}")
        return

    # Static defaults written before researcher.py overwrites with actual values
    data = [
        {"range": f"'{sheet_name}'!AB{r}", "values": [[0.1325]]},  # eBay fee rate default
        {"range": f"'{sheet_name}'!AD{r}", "values": [[0]]},       # ship cost default (free)
    ]
    try:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
    except Exception as e:
        logger.warning(f"seed_formula_row: static default write failed for row {r}: {e}")
        return

    logger.debug(f"  Formula row seeded: row {r} ({sheet_name})")
