"""
Import an eBay File Exchange "results" CSV back into the Google Sheet.

After you upload the Seller Hub CSV to eBay, eBay returns a results file (the one
with Line Number / Action / Status / ItemID / CustomLabel columns). This script
reads it and, for every row that listed successfully, writes the listing URL into
col Q and flips the row ACTIVE — so the bot knows it's live.

Identity: the results CSV's CustomLabel is a stable SKU (or a legacy "ROW<sheet_row>"
from older exports). SKUs are resolved to the CURRENT sheet row at import time, so a
row that shifted after an audit delete still gets its URL written to the right product.

Usage (from the project root, on Windows where the Google creds live):

    python tools/import_ebay_results.py path/to/results.csv            # dry run
    python tools/import_ebay_results.py path/to/results.csv --apply    # write

Rows that already have a col Q URL are skipped. Failures are reported, never written.
Dry run is the default — nothing touches the sheet unless you pass --apply.
"""

import argparse
import csv
import io
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

_ROW_RE = re.compile(r"^ROW(\d+)$")


def resolve_row(label):
    """'ROW42' -> 42 (a sheet row). Anything else -> None (resolve by SKU instead)."""
    m = _ROW_RE.match(label or "")
    return int(m.group(1)) if m else None


def load_col_map():
    with open(_BASE / "config" / "col_map.yaml") as f:
        return yaml.safe_load(f)["columns"]


def load_sheet_name():
    with open(_BASE / "config" / "categories.yaml") as f:
        cfg = yaml.safe_load(f)
    return cfg["business"]["sheet_name"]


def _parse_stream(f):
    successes, failures = [], []
    for row in csv.DictReader(f):
        label = (row.get("CustomLabel") or "").strip()
        status = (row.get("Status") or "").strip()
        item_id = (row.get("ItemID") or "").strip()
        if status == "Success" and item_id and label:
            successes.append((label, item_id))
        elif status == "Failure":
            err = (row.get("ErrorMessage") or "").strip()
            failures.append((label or None, err or f"row {label}"))
    return successes, failures


def parse_results(path):
    """Return (successes, failures) from a results CSV file path.

    successes: list of (label:str, item_id:str) — label is the CustomLabel
               (a SKU, or a legacy "ROW<sheet_row>").
    failures:  list of (label:str|None, error:str)
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        return _parse_stream(f)


def parse_results_text(text):
    """Return (successes, failures) from raw results CSV content (e.g. pasted into Telegram)."""
    return _parse_stream(io.StringIO(text))


def _col_idx(letter):
    n = 0
    for c in letter.upper():
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


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
    sku_col = col_map["sku"]                # AA

    successes, failures = parse_results(args.results_csv)
    print(f"eBay results: {len(successes)} listed OK, {len(failures)} failed")
    for label, err in failures:
        print(f"  ✗ {label or '?'}: {err[:120]}")

    if not successes:
        print("Nothing to import.")
        return

    if not args.apply:
        print("\nDRY RUN — would resolve + write (add --apply to commit):")
        for label, item_id in successes:
            print(f"  {label}: {url_col} = https://www.ebay.com/itm/{item_id}  →  {status_col}=ACTIVE, {platform_col}=eBay")
        return

    from tools.sheet_writer import get_sheets_service, read_sheet, safe_write_row  # local: needs google libs
    service = get_sheets_service()
    start = 4
    rows = read_sheet(service, f"'{sheet_name}'!A{start}:BA500")

    sku_i = _col_idx(sku_col)
    url_i = _col_idx(url_col)

    # SKU -> current sheet row, from the live sheet.
    sku_to_row = {}
    for offset, r in enumerate(rows):
        sku = (r[sku_i] if sku_i < len(r) else "").strip()
        if sku:
            sku_to_row[sku] = start + offset

    def existing_url(row_num):
        idx = row_num - start
        if idx < 0 or idx >= len(rows):
            return ""
        r = rows[idx]
        return (r[url_i] if url_i < len(r) else "").strip()

    written = skipped = unresolved = 0
    for label, item_id in successes:
        row_num = resolve_row(label)
        if row_num is None:
            row_num = sku_to_row.get(label)
        if row_num is None:
            unresolved += 1
            print(f"  ⚠️ {label}: no matching row (SKU not found) — skipped")
            continue
        if existing_url(row_num):
            skipped += 1
            continue
        safe_write_row(service, sheet_name, row_num, [
            (status_col, "ACTIVE"),
            (platform_col, "eBay"),
            (url_col, f"https://www.ebay.com/itm/{item_id}"),
        ])
        written += 1

    print(f"\nDone: wrote {written} rows ACTIVE, skipped {skipped} (already listed), {unresolved} unresolved.")


if __name__ == "__main__":
    main()
