"""
Import an eBay File Exchange "results" CSV back into the Google Sheet.

After you upload the Seller Hub CSV to eBay, eBay emails/downloads a results file
(the one with Line Number / Action / Status / ItemID / CustomLabel columns). This
script reads that file and, for every row that listed successfully, writes the
listing URL into col Q and flips the row ACTIVE — so the bot knows it's live
instead of sitting at READY with no eBay link.

Usage (run from the project root, on Windows where the Google creds live):

    python tools/import_ebay_results.py path/to/results.csv            # dry run
    python tools/import_ebay_results.py path/to/results.csv --apply    # write

The results CSV uses CustomLabel = "ROW<sheet_row>" (the exporter writes the sheet
row number there), so row matching is exact and can't drift. Rows that already have
a col Q URL are skipped. Failures are reported but never written.

Dry run is the default — nothing touches the sheet unless you pass --apply.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

_ROW_RE = re.compile(r"^ROW(\d+)$")


def load_col_map():
    with open(_BASE / "config" / "col_map.yaml") as f:
        return yaml.safe_load(f)["columns"]


def load_sheet_name():
    with open(_BASE / "config" / "categories.yaml") as f:
        cfg = yaml.safe_load(f)
    return cfg["business"]["sheet_name"]


def parse_results(path):
    """Return (successes, failures).

    successes: list of (sheet_row:int, item_id:str)
    failures:  list of (sheet_row:int|None, error:str)
    """
    successes, failures = [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            label = (row.get("CustomLabel") or "").strip()
            m = _ROW_RE.match(label)
            sheet_row = int(m.group(1)) if m else None
            status = (row.get("Status") or "").strip()
            item_id = (row.get("ItemID") or "").strip()
            if status == "Success" and item_id and sheet_row:
                successes.append((sheet_row, item_id))
            elif status == "Failure":
                err = (row.get("ErrorMessage") or "").strip()
                failures.append((sheet_row, err or f"row {label}"))
    return successes, failures


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("results_csv")
    ap.add_argument("--apply", action="store_true", help="write to the sheet (default is dry run)")
    args = ap.parse_args()

    col_map = load_col_map()
    sheet_name = load_sheet_name()
    status_col = col_map["status"]          # A
    platform_col = col_map["platform"]      # E
    url_col = col_map["ebay_listing_url"]   # Q

    successes, failures = parse_results(args.results_csv)

    print(f"eBay results: {len(successes)} listed OK, {len(failures)} failed")
    for sheet_row, err in failures:
        print(f"  ✗ row {sheet_row}: {err[:120]}")

    if not successes:
        print("Nothing to import.")
        return

    if not args.apply:
        print("\nDRY RUN — would write (add --apply to commit):")
        for sheet_row, item_id in successes:
            print(f"  row {sheet_row}: {url_col} = https://www.ebay.com/itm/{item_id}  →  {status_col}=ACTIVE, {platform_col}=eBay")
        return

    from tools.sheet_writer import get_sheets_service, safe_write_row  # local: needs google libs
    service = get_sheets_service()
    # Read current col Q (and A/E) for the target rows so we never clobber an
    # already-listed row. Range A{min}:Q{max} covers status(A)..url(Q).
    rows_needed = [r for r, _ in successes]
    lo, hi = min(rows_needed), max(rows_needed)
    read_range = f"'{sheet_name}'!A{lo}:Q{hi}"
    existing = service.spreadsheets().values().get(
        spreadsheetId=_env_sheet_id(), range=read_range
    ).execute().get("values", [])

    def cell(row_num, letter):
        idx = ord(letter) - ord("A")
        offset = row_num - lo
        if offset < 0 or offset >= len(existing):
            return ""
        r = existing[offset]
        return r[idx] if idx < len(r) else ""

    written, skipped = 0, 0
    for sheet_row, item_id in successes:
        if cell(sheet_row, url_col).strip():
            skipped += 1
            continue
        url = f"https://www.ebay.com/itm/{item_id}"
        safe_write_row(service, sheet_name, sheet_row, [
            (status_col, "ACTIVE"),
            (platform_col, "eBay"),
            (url_col, url),
        ])
        written += 1

    print(f"\nDone: wrote {written} rows ACTIVE, skipped {skipped} (already had a URL).")


def _env_sheet_id():
    import os
    return os.getenv("GOOGLE_SHEET_ID")


if __name__ == "__main__":
    main()
