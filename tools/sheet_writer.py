"""
Tool: sheet_writer
Handles all Google Sheets read/write operations.
Secrets loaded from .env — never hardcoded.
"""

import os
import socket
import time

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

socket.setdefaulttimeout(60)  # 60-second cap on all socket reads including Sheets API

# Google caps Sheets writes at 60/min per service account. 429 (rate limit) and
# transient 500/503 are retried with exponential backoff before giving up.
RETRY_STATUSES = (429, 500, 503)
RETRY_DELAYS   = (1, 2, 4, 8, 16)  # seconds before retry 1..5


def execute_with_retry(request, label="sheets call"):
    """
    Run a googleapiclient request's .execute(), retrying on HttpError with
    status in RETRY_STATUSES and on socket.timeout, up to len(RETRY_DELAYS)
    times with exponential backoff. Any other error (403, 400, ...) raises
    immediately; the last error raises once retries are exhausted.
    Wrap the individual .execute() rather than a whole read-then-write
    function so a retried 5xx can't re-read state and double-append.
    """
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return request.execute()
        except (HttpError, socket.timeout) as e:
            status = e.resp.status if isinstance(e, HttpError) else "timeout"
            retryable = isinstance(e, socket.timeout) or status in RETRY_STATUSES
            if not retryable or attempt >= len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[attempt]
            logger.warning(
                f"Sheets {label}: {status} — retry {attempt + 1}/{len(RETRY_DELAYS)} in {delay}s"
            )
            time.sleep(delay)


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
    execute_with_retry(service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=range_addr,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]}
    ), "write_cell")


def append_row(service, sheet_name, col_value_dict, COL, data_start_row=4):
    """
    Appends a new row to the sheet with values at specific columns.
    col_value_dict: {col_letter: value, ...}  e.g. {"A": "title", "C": "Jewelry"}
    Never writes above data_start_row (rows 1-3 are reserved for dashboard headers).
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID")

    # Find the next empty row, but never above data_start_row
    result = execute_with_retry(service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!A:A",
    ), "append_row read")
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
        execute_with_retry(service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
), "append_row")
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
    result = execute_with_retry(service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!A:A",
    ), "append_rows_batch read")
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
        execute_with_retry(service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
), "append_rows_batch")

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
        execute_with_retry(service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data}
        ), "write_row_partial")
