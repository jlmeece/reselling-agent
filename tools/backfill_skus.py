"""
Backfill stable SKUs (col AA) for every tracked row that doesn't have one yet.

The bot historically tracked products by sheet ROW NUMBER, which breaks whenever the
audit deletes a row and shifts everything below it. The fix is a stable SKU per row:

  - Preferred: the Costco product ID extracted from col R (the source URL, e.g.
    "https://www.costco.com/.product.1700000.html" -> "1700000"). Unique + permanent.
  - Fallback: a generated unique ID (e.g. "X4f2a9c1b3d") for rows with no source URL.

Idempotent — rows that already have a SKU are left alone, so it can be re-run after
new rows appear. Dry run is the default: it prints exactly what it would write and
touches nothing until you pass --apply.

Usage (from the project root, on Windows where the Google creds live):

    python tools/backfill_skus.py            # dry run — preview the assignments
    python tools/backfill_skus.py --apply    # write SKUs to col AA
"""

import argparse
import re
import sys
import uuid
from pathlib import Path

import yaml
from dotenv import load_dotenv

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

_PRODUCT_ID_RE = re.compile(r"\.product\.(\d+)")


def extract_product_id(url):
    m = _PRODUCT_ID_RE.search(url or "")
    return m.group(1) if m else None


def load_col_map():
    with open(_BASE / "config" / "col_map.yaml") as f:
        return yaml.safe_load(f)["columns"]


def load_sheet_name():
    with open(_BASE / "config" / "categories.yaml") as f:
        return yaml.safe_load(f)["business"]["sheet_name"]


def _col_idx(letter):
    n = 0
    for c in letter.upper():
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write SKUs to the sheet (default is dry run)")
    args = ap.parse_args()

    col_map = load_col_map()
    sheet_name = load_sheet_name()
    status_col = col_map["status"]        # A
    url_col = col_map["costco_url"]       # R
    sku_col = col_map["sku"]              # AA

    from tools.sheet_writer import get_sheets_service, read_sheet, safe_write_row
    service = get_sheets_service()
    start = 4
    rows = read_sheet(service, f"'{sheet_name}'!A{start}:AA500")

    status_i = _col_idx(status_col)
    url_i = _col_idx(url_col)
    sku_i = _col_idx(sku_col)

    to_assign = []  # (row_num, sku, source)
    already = 0
    for offset, r in enumerate(rows):
        status = (r[status_i] if status_i < len(r) else "").strip()
        if not status:
            continue  # header / blank row
        existing_sku = (r[sku_i] if sku_i < len(r) else "").strip()
        if existing_sku:
            already += 1
            continue
        url = (r[url_i] if url_i < len(r) else "").strip()
        pid = extract_product_id(url)
        if pid:
            to_assign.append((start + offset, pid, "costco-id"))
        else:
            to_assign.append((start + offset, f"X{uuid.uuid4().hex[:10]}", "generated"))

    print(f"Rows reviewed: {len(rows)} · already have SKU: {already} · to assign: {len(to_assign)}")

    if not to_assign:
        print("Nothing to do.")
        return

    for row_num, sku, source in to_assign:
        print(f"  row {row_num}: {sku_col} = {sku}  ({source})")

    if not args.apply:
        print("\nDRY RUN — add --apply to write these SKUs to the sheet.")
        return

    for row_num, sku, _ in to_assign:
        safe_write_row(service, sheet_name, row_num, [(sku_col, sku)])
    print(f"\nDone: wrote {len(to_assign)} SKUs.")


if __name__ == "__main__":
    main()
