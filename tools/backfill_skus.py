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
# Modern Costco URLs: https://www.costco.com/p/-/<slug>/<9-10 digit id>. The old
# backfill only parsed the `.product.<id>.html` form, so rows with the `/p/` form
# got placeholder "X…" SKUs instead of their real Costco product ID.
_TRAILING_ID_RE = re.compile(r"(\d{7,12})(?:[/?#].*)?$")


def extract_product_id(url):
    url = (url or "").strip()
    if not url:
        return None
    m = _PRODUCT_ID_RE.search(url)
    if m:
        return m.group(1)
    m = _TRAILING_ID_RE.search(url)
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


def plan_assignments(rows, col_map, start=4):
    """Pure: -> (to_assign [(row_num, sku, source, url)], already). A row with a status and a
    blank col AA gets its Costco product id from col R (either URL shape), else a generated
    "X…" id. Rows that already have a SKU are left alone (idempotent)."""
    status_i = _col_idx(col_map["status"])
    url_i = _col_idx(col_map["costco_url"])
    sku_i = _col_idx(col_map["sku"])
    to_assign, already = [], 0
    for offset, r in enumerate(rows):
        status = (r[status_i] if status_i < len(r) else "").strip()
        if not status:
            continue  # header / blank row
        if (r[sku_i] if sku_i < len(r) else "").strip():
            already += 1
            continue
        url = (r[url_i] if url_i < len(r) else "").strip()
        pid = extract_product_id(url)
        if pid:
            to_assign.append((start + offset, pid, "costco-id", url))
        else:
            to_assign.append((start + offset, f"X{uuid.uuid4().hex[:10]}", "generated", url))
    return to_assign, already


def backfill_skus(service, sheet_name, col_map=None, *, start=4, end=500, apply=True, verbose=False) -> int:
    """Fill blank col AA SKUs; -> number of SKUs written (planned, when apply=False).
    Rows are re-read right before writing and a row whose col R changed since the plan is
    skipped, so a row shift (auditor delete, bot, another run) can't put a SKU on the wrong row."""
    from tools.sheet_writer import read_sheet, safe_write_row
    col_map = col_map or load_col_map()
    rng = f"'{sheet_name}'!A{start}:AA{end}"
    to_assign, already = plan_assignments(read_sheet(service, rng), col_map, start)
    if verbose:
        print(f"Rows reviewed · already have SKU: {already} · to assign: {len(to_assign)}")
        for row_num, sku, source, _ in to_assign:
            print(f"  row {row_num}: {col_map['sku']} = {sku}  ({source})")
    if not apply or not to_assign:
        return len(to_assign) if not apply else 0
    fresh = read_sheet(service, rng)
    url_i, sku_i = _col_idx(col_map["costco_url"]), _col_idx(col_map["sku"])
    written = 0
    for row_num, sku, _, url in to_assign:
        r = fresh[row_num - start] if row_num - start < len(fresh) else []
        if (r[url_i] if url_i < len(r) else "").strip() != url or (r[sku_i] if sku_i < len(r) else "").strip():
            continue   # row moved or got a SKU since the plan
        safe_write_row(service, sheet_name, row_num, [(col_map["sku"], sku)])
        written += 1
    return written


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write SKUs to the sheet (default is dry run)")
    args = ap.parse_args()

    from tools.sheet_writer import get_sheets_service
    n = backfill_skus(get_sheets_service(), load_sheet_name(), apply=args.apply, verbose=True)
    if not n:
        print("Nothing to do.")
    elif not args.apply:
        print("\nDRY RUN — add --apply to write these SKUs to the sheet.")
    else:
        print(f"\nDone: wrote {n} SKUs.")


if __name__ == "__main__":
    main()
