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


def execute_with_retry(request, label="sheets call", retry_statuses=RETRY_STATUSES,
                       retry_timeouts=True):
    """
    Run a googleapiclient request's .execute(), retrying on HttpError with
    status in retry_statuses and (if retry_timeouts) on socket.timeout, up to
    len(RETRY_DELAYS) times with exponential backoff. Any other error
    (403, 400, ...) raises immediately; the last error raises once retries
    are exhausted.
    Wrap the individual .execute() rather than a whole read-then-write
    function so a retried 5xx can't re-read state and double-append.
    Non-idempotent writes (deleteDimension, addSheet) must pass
    retry_statuses=(429,), retry_timeouts=False: a 429 was rejected outright,
    but a 5xx or timeout may have landed, and a retry would repeat the effect.
    """
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return request.execute()
        except (HttpError, socket.timeout) as e:
            status = e.resp.status if isinstance(e, HttpError) else "timeout"
            retryable = (retry_timeouts if isinstance(e, socket.timeout)
                         else status in retry_statuses)
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


def _letter_to_idx(letters):
    """'A' -> 0, 'Z' -> 25, 'AA' -> 26, 'BA' -> 52."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def required_grid_columns(col_map=None):
    """Column count the tab's grid must have for every col_map.yaml column to be
    writable (rightmost column index + 1). Loads config/col_map.yaml if col_map
    is None."""
    if col_map is None:
        import yaml
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "col_map.yaml")
        with open(path) as f:
            col_map = yaml.safe_load(f)["columns"]
    return max(_letter_to_idx(v) for v in col_map.values()) + 1


def ensure_grid_columns(service, sheet_name, min_cols):
    """Expand the tab's grid to at least min_cols columns; never shrinks.
    Sheets rejects writes past the grid ('exceeds grid limits'), so a column
    added to col_map.yaml needs the grid to grow with it. Setting an absolute
    columnCount is idempotent, so the default retry policy is safe. Returns True
    if it expanded."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    meta = execute_with_retry(service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets.properties(sheetId,title,gridProperties.columnCount)",
    ), "grid_meta")
    for s in meta.get("sheets", []):
        props = s["properties"]
        if props.get("title") == sheet_name:
            break
    else:
        raise ValueError(f"Tab {sheet_name!r} not found")
    current = props.get("gridProperties", {}).get("columnCount", 0)
    if current >= min_cols:
        return False
    execute_with_retry(service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": props["sheetId"],
                           "gridProperties": {"columnCount": min_cols}},
            "fields": "gridProperties.columnCount",
        }}]},
    ), "grid_expand")
    logger.info(f"Expanded '{sheet_name}' grid from {current} to {min_cols} columns")
    return True


def read_sheet(service, range_name):
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    result = execute_with_retry(service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=range_name
    ), "read_sheet")
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


# config/col_map.yaml's own header comment documents these as formula columns
# (net_profit, net_margin, comp_saturation, total_cost, ebay_fees, tax_est,
# site_profit, ad_budget) — never overwrite them with an agent/bot write.
PROTECTED_COLS = {"I", "J", "N", "Z", "AC", "AF", "AG", "AH"}


def safe_write_row(service, sheet_name, row_num, col_value_pairs):
    """
    Wraps write_row_partial with a hard stop against ever writing to a formula
    column. Raises ValueError rather than silently dropping the offending pair —
    a silent drop would look like a successful write to the caller while
    quietly doing nothing, which is worse than a loud failure for a
    money-affecting sheet. Every bot/sync write-back action must go through
    this, never write_row_partial directly.
    """
    bad = [col for col, _ in col_value_pairs if col.upper() in PROTECTED_COLS]
    if bad:
        raise ValueError(f"Refusing to write protected formula column(s): {bad}")
    return write_row_partial(service, sheet_name, row_num, col_value_pairs)
