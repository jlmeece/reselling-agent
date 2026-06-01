"""
Tool: run_logger
Appends a structured record of each scheduler run to:
  1. data/run_history.json  — local audit trail
  2. Google Sheet "Run Log" tab — always-visible status dashboard

Usage in scheduler.py:
    from tools.run_logger import log_run_start, log_run_end

    start = log_run_start("research")
    # ... do work, build results dict ...
    log_run_end("research", start, results, service)

results dict keys (all optional):
    status          "ok" | "error" | "skipped"
    new_products    int
    researched      int
    tier1           int
    tier2           int
    tier3           int
    scout_health    str  e.g. "Reddit OK / YouTube quota / CSE 403"
    errors          str  brief error description if status="error"
    spot_gold       float
    spot_silver     float
    notes           str  any extra context
"""

import json
import os
import time
from datetime import datetime
from loguru import logger


_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_HISTORY_FILE = os.path.join(_DATA_DIR, "run_history.json")
_RUN_LOG_TAB  = "Run Log"
_MAX_HISTORY  = 500   # trim json to last N runs


def log_run_start(mode: str) -> float:
    """Call at the top of each run. Returns start timestamp for duration tracking."""
    logger.debug(f"run_logger: [{mode}] started at {datetime.now().strftime('%H:%M:%S')}")
    return time.time()


def log_run_end(mode: str, start_time: float, results: dict, service=None):
    """
    Call at the bottom of each run. Writes to run_history.json and the Run Log sheet tab.

    service: googleapiclient sheets service (optional — skips sheet write if None)
    """
    duration_s = int(time.time() - start_time)
    duration_str = f"{duration_s // 60}m {duration_s % 60}s" if duration_s >= 60 else f"{duration_s}s"

    now = datetime.now()
    status = results.get("status", "ok")

    entry = {
        "date":          now.strftime("%Y-%m-%d"),
        "time":          now.strftime("%H:%M"),
        "mode":          mode,
        "status":        status,
        "duration":      duration_str,
        "new_products":  results.get("new_products", ""),
        "researched":    results.get("researched", ""),
        "tier1":         results.get("tier1", ""),
        "tier2":         results.get("tier2", ""),
        "tier3":         results.get("tier3", ""),
        "scout_health":  results.get("scout_health", ""),
        "spot_gold":     results.get("spot_gold", ""),
        "spot_silver":   results.get("spot_silver", ""),
        "errors":        results.get("errors", ""),
        "notes":         results.get("notes", ""),
    }

    _append_json(entry)

    if service:
        _append_sheet(entry, service)
    else:
        logger.debug("run_logger: no sheets service provided — skipping sheet write")

    icon = "✓" if status == "ok" else ("⚠" if status == "skipped" else "✗")
    logger.info(f"run_logger: [{mode}] {icon} logged — {duration_str}")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _append_json(entry: dict):
    try:
        if not os.path.exists(_HISTORY_FILE):
            data = {"description": "Append-only log of each agent run. Never delete entries.", "runs": []}
        else:
            with open(_HISTORY_FILE) as f:
                data = json.load(f)

        data["runs"].append(entry)

        # Trim to last N entries
        if len(data["runs"]) > _MAX_HISTORY:
            data["runs"] = data["runs"][-_MAX_HISTORY:]

        with open(_HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"run_logger: json write failed: {e}")


def _truncate_error(text: str, max_len: int = 120) -> str:
    """Keep only the last line of a Python traceback, capped at max_len chars."""
    if not text:
        return text
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    last  = lines[-1] if lines else text
    return last[:max_len]


def _append_sheet(entry: dict, service):
    """Appends one row to the 'Run Log' sheet tab. Creates the tab + header if missing."""
    try:
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        if not sheet_id:
            return

        spreadsheet = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        tab_names = [s["properties"]["title"] for s in spreadsheet.get("sheets", [])]

        if _RUN_LOG_TAB not in tab_names:
            _create_run_log_tab(service, sheet_id)

        # Dedup: skip if the last row already has same date+time+mode
        existing = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{_RUN_LOG_TAB}'!A:C",
        ).execute().get("values", [])
        if len(existing) > 1:
            last_row = existing[-1]
            if (len(last_row) >= 3 and
                    last_row[0] == entry["date"] and
                    last_row[1] == entry["time"] and
                    last_row[2] == entry["mode"]):
                logger.debug("run_logger: duplicate entry skipped — same date/time/mode already logged")
                return

        status_icon = "✓ OK" if entry["status"] == "ok" else ("⚠ Skipped" if entry["status"] == "skipped" else "✗ Error")
        spot_str = ""
        if entry.get("spot_gold"):
            spot_str = f"Gold ${entry['spot_gold']}/oz"
            if entry.get("spot_silver"):
                spot_str += f" | Silver ${entry['spot_silver']}/oz"

        notes_col = entry.get("notes", "")
        if spot_str:
            notes_col = f"{spot_str} | {notes_col}" if notes_col else spot_str

        error_col = _truncate_error(entry.get("errors", ""))

        row = [
            entry["date"],
            entry["time"],
            entry["mode"],
            status_icon,
            entry["duration"],
            entry.get("new_products", ""),
            entry.get("researched", ""),
            entry.get("tier1", ""),
            entry.get("tier2", ""),
            entry.get("scout_health", ""),
            error_col or notes_col,
        ]

        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{_RUN_LOG_TAB}'!A:K",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    except Exception as e:
        logger.warning(f"run_logger: sheet append failed: {e}")


def _create_run_log_tab(service, sheet_id: str):
    """Creates the Run Log tab and writes column headers."""
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": _RUN_LOG_TAB}}}]},
    ).execute()

    headers = [["Date", "Time", "Mode", "Status", "Duration",
                 "New", "Researched", "T1", "T2", "Scout Health", "Notes / Errors"]]
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{_RUN_LOG_TAB}'!A1:K1",
        valueInputOption="RAW",
        body={"values": headers},
    ).execute()
    logger.info(f"run_logger: created '{_RUN_LOG_TAB}' tab with headers")
