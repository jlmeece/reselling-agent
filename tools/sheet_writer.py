"""
Tool: sheet_writer
Handles all Google Sheets read/write operations.
Secrets loaded from .env — never hardcoded.
"""

import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


def get_sheets_service():
    creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    return build("sheets", "v4", credentials=creds)


def read_sheet(service, range_name):
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=range_name
    ).execute()
    return result.get("values", [])


def write_cell(service, sheet_name, col, row, value):
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    range_addr = f"'{sheet_name}'!{col}{row}"
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=range_addr,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]}
    ).execute()


def append_row(service, sheet_name, col_value_dict, COL, data_start_row=4):
    """
    Appends a new row to the sheet with values at specific columns.
    col_value_dict: {col_letter: value, ...}  e.g. {"A": "title", "C": "Jewelry"}
    Never writes above data_start_row (rows 1-3 are reserved for dashboard headers).
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID")

    # Find the next empty row, but never above data_start_row
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!A:A",
    ).execute()
    next_row = max(len(result.get("values", [])) + 1, data_start_row)

    data = []
    for col, value in col_value_dict.items():
        # {ROW} placeholder lets callers write row-aware formulas
        # e.g. "=IFERROR(M{ROW}/MAX(K{ROW},1),\"\")"
        if isinstance(value, str) and "{ROW}" in value:
            value = value.replace("{ROW}", str(next_row))
        data.append({
            "range": f"'{sheet_name}'!{col}{next_row}",
            "values": [[value]],
        })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
    return next_row


def append_rows_batch(service, sheet_name, col_value_dicts, data_start_row=4):
    """
    Appends multiple rows to the sheet in a single batchUpdate API call.
    col_value_dicts: list of {col_letter: value, ...} dicts (same format as append_row)
    Returns list of row numbers written.
    """
    if not col_value_dicts:
        return []

    sheet_id = os.getenv("GOOGLE_SHEET_ID")

    # Read current row count once
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!A:A",
    ).execute()
    start_row = max(len(result.get("values", [])) + 1, data_start_row)

    # Build all ranges for all rows in one pass
    data = []
    row_numbers = []
    for i, col_value_dict in enumerate(col_value_dicts):
        row_num = start_row + i
        row_numbers.append(row_num)
        for col, value in col_value_dict.items():
            if isinstance(value, str) and "{ROW}" in value:
                value = value.replace("{ROW}", str(row_num))
            data.append({
                "range": f"'{sheet_name}'!{col}{row_num}",
                "values": [[value]],
            })

    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()

    return row_numbers


def write_row_partial(service, sheet_name, row_num, col_value_pairs):
    """Write multiple non-contiguous cells in one row in a single API call."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    data = []
    for col, value in col_value_pairs:
        data.append({
            "range": f"'{sheet_name}'!{col}{row_num}",
            "values": [[value]]
        })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data}
        ).execute()
