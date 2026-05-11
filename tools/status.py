"""
Tool: status
On-demand agent status snapshot. Run from VS Code terminal any time.

    python tools/status.py

Shows: last run per mode, product counts by status, live spot prices,
next scheduled run, and any recent errors.
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

# ── Schedule reference (Central time, CDT) ────────────────────────────────────
_SCHEDULE = {
    "discovery": {"hour": 7,  "minute": 0,  "days": "daily"},
    "active_1":  {"hour": 8,  "minute": 0,  "days": "daily"},
    "daily":     {"hour": 9,  "minute": 0,  "days": "daily"},
    "research":  {"hour": 10, "minute": 0,  "days": "daily"},
    "active_2":  {"hour": 13, "minute": 0,  "days": "daily"},
    "active_3":  {"hour": 18, "minute": 0,  "days": "daily"},
    "rotation":  {"hour": 9,  "minute": 0,  "days": "Friday"},
}

_STATUS_ORDER = ["ACTIVE", "APPROVED", "READY", "WATCH", "PENDING",
                 "PAUSED_OOS", "PAUSED_MARGIN", "PAUSED_DEMAND", "REJECTED"]


def _load_history():
    """Load run history — Sheet Run Log tab first, local JSON as fallback."""
    sheet_runs = _sheet_run_log()
    if sheet_runs:
        return sheet_runs

    # Fallback: local file (only populated by local runs, not GitHub Actions)
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "run_history.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f).get("runs", [])


def _sheet_run_log():
    """Read the Run Log tab from Google Sheet. Returns list of run dicts, or [] on failure."""
    try:
        import yaml
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "categories.yaml")
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)

        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
        sheet_id   = os.getenv("GOOGLE_SHEET_ID")

        creds   = Credentials.from_service_account_file(
            creds_file, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        service = build("sheets", "v4", credentials=creds)
        result  = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="'Run Log'!A2:K500"
        ).execute()
        rows = result.get("values", [])

        runs = []
        for row in rows:
            if len(row) < 3:
                continue
            status_cell = row[3] if len(row) > 3 else ""
            if "OK" in status_cell or "✓" in status_cell:
                status = "ok"
            elif "Skip" in status_cell or "⚠" in status_cell:
                status = "skipped"
            else:
                status = "error"

            runs.append({
                "date":     row[0] if len(row) > 0 else "",
                "time":     row[1] if len(row) > 1 else "",
                "mode":     row[2] if len(row) > 2 else "",
                "status":   status,
                "duration": row[4] if len(row) > 4 else "",
                "errors":   row[10] if len(row) > 10 else "",
            })
        return runs
    except Exception:
        return []


def _last_runs(history):
    """Return the most recent entry per mode."""
    seen = {}
    for run in reversed(history):
        mode = run.get("mode")
        if mode and mode not in seen:
            seen[mode] = run
    return seen


def _time_ago(date_str, time_str):
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        delta = datetime.now() - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins}m ago"
        elif mins < 1440:
            return f"{mins // 60}h ago"
        else:
            return f"{mins // 1440}d ago"
    except Exception:
        return "?"


def _next_run():
    """Return the label and time-until for the next scheduled run (CDT)."""
    now = datetime.now()
    candidates = []
    for label, sched in _SCHEDULE.items():
        if sched["days"] == "Friday" and now.weekday() != 4:
            days_until = (4 - now.weekday()) % 7 or 7
            run_dt = now.replace(hour=sched["hour"], minute=sched["minute"], second=0, microsecond=0)
            run_dt += timedelta(days=days_until)
        else:
            run_dt = now.replace(hour=sched["hour"], minute=sched["minute"], second=0, microsecond=0)
            if run_dt <= now:
                run_dt += timedelta(days=1)
        candidates.append((run_dt, label))
    candidates.sort()
    next_dt, next_label = candidates[0]
    delta = next_dt - now
    mins = int(delta.total_seconds() / 60)
    if mins < 60:
        until = f"in {mins}m"
    else:
        until = f"in {mins // 60}h {mins % 60}m"
    return next_label, next_dt.strftime("%I:%M %p CDT"), until


def _sheet_status_counts():
    """Read the Google Sheet and count products by status."""
    try:
        import yaml
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "categories.yaml")
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)

        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
        sheet_id   = os.getenv("GOOGLE_SHEET_ID")
        sheet_name = cfg["business"]["sheet_name"]
        start_row  = cfg["business"]["data_start_row"]
        end_row    = cfg["business"]["data_end_row"]

        creds   = Credentials.from_service_account_file(creds_file, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        service = build("sheets", "v4", credentials=creds)
        result  = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{sheet_name}'!A{start_row}:C{end_row}"
        ).execute()
        rows = result.get("values", [])

        counts = {}
        titles = {}
        for row in rows:
            status = row[0].strip() if row else ""
            title  = row[2].strip() if len(row) > 2 else ""
            if not status:
                continue
            counts[status] = counts.get(status, 0) + 1
            titles.setdefault(status, []).append(title[:35])

        return counts, titles
    except Exception as e:
        return {}, {}


def _spot_prices():
    try:
        from tools.spot_price import get_spot_price
        gold   = get_spot_price("gold")
        silver = get_spot_price("silver")
        return gold, silver
    except Exception:
        return None, None


def main():
    W = "\033[0m"
    B = "\033[1m"
    G = "\033[32m"
    Y = "\033[33m"
    R = "\033[31m"
    C = "\033[36m"
    DIM = "\033[2m"

    print(f"\n{B}{'=' * 52}{W}")
    print(f"{B}  WAT Agent Status  {datetime.now().strftime('%Y-%m-%d %H:%M')}{W}")
    print(f"{B}{'=' * 52}{W}\n")

    # ── Run history ──────────────────────────────────────────────────────────
    history = _load_history()
    last    = _last_runs(history)

    if history:
        print(f"{B}Last runs:{W}")
        mode_order = ["discovery", "research", "daily", "active", "rotation"]
        printed = set()
        for mode in mode_order:
            run = last.get(mode)
            if run and mode not in printed:
                ago  = _time_ago(run["date"], run["time"])
                icon = G + "✓" if run["status"] == "ok" else (Y + "⚠" if run["status"] == "skipped" else R + "✗")
                dur  = run.get("duration", "")
                err  = f"  {R}{run['errors']}{W}" if run.get("errors") else ""
                print(f"  {C}{mode:<12}{W} {run['date']} {run['time']}  {icon}{W} {dur:<8} {DIM}({ago}){W}{err}")
                printed.add(mode)
        if not printed:
            print(f"  {DIM}No runs recorded yet{W}")
    else:
        print(f"{B}Last runs:{W}")
        print(f"  {DIM}No runs recorded yet (check GitHub Actions UI or Sheet 'Run Log' tab){W}")

    # ── Sheet status counts ──────────────────────────────────────────────────
    print(f"\n{B}Sheet: Product Tracker{W}")
    counts, titles = _sheet_status_counts()
    if counts:
        for status in _STATUS_ORDER:
            n = counts.get(status, 0)
            if n == 0:
                continue
            color = G if status == "ACTIVE" else (Y if status in ("WATCH", "PENDING", "APPROVED", "READY") else DIM)
            sample = titles.get(status, [])
            sample_str = f"{DIM}  ({', '.join(sample[:2])}{'…' if len(sample) > 2 else ''}){W}" if sample else ""
            print(f"  {color}{status:<16}{W} {B}{n}{W}{sample_str}")
        other = {k: v for k, v in counts.items() if k not in _STATUS_ORDER}
        for status, n in other.items():
            print(f"  {DIM}{status:<16}{W} {n}")
        if not any(counts.values()):
            print(f"  {DIM}Sheet appears empty{W}")
    else:
        print(f"  {DIM}Could not read sheet (check credentials){W}")

    # ── Spot prices ──────────────────────────────────────────────────────────
    print(f"\n{B}Spot prices:{W}")
    gold, silver = _spot_prices()
    if gold:
        print(f"  Gold   {G}${gold:,.2f}/oz{W}    Silver  {G}${silver:,.2f}/oz{W}")
    else:
        print(f"  {DIM}Unavailable (Yahoo Finance timeout){W}")

    # ── Next scheduled run ───────────────────────────────────────────────────
    next_label, next_time, until = _next_run()
    print(f"\n{B}Next scheduled run:{W}  {C}{next_label}{W} @ {next_time}  {DIM}({until}){W}\n")


if __name__ == "__main__":
    main()
