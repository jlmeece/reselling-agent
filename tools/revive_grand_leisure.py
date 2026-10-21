"""
One-off: bring Grand Leisure City Heights + Las Palmas back into the pipeline.

The auditor moved both rows to the Graveyard (10/17, "Zero velocity + net < $5") while their eBay
listings were still live; the 10/19 orphan triage hid those listings (quantity 0). This script
re-activates each hidden listing (ReviseInventoryStatus quantity 5, via ebay_sync.set_quantity)
and re-links it to an ACTIVE Product Tracker row (updates the row with the exact title, else
appends one), writing ONLY cols A / C / D / H / Q / R / AA.

Match guard: a product is revived only when exactly ONE live listing's title contains its name;
0 or >1 matches = reported and skipped (never raise quantity on a guess). Idempotent: setting the
same quantity again is safe, and a re-run updates the row it appended the first time.

Spec: docs/superpowers/specs/2026-10-20-revive-grand-leisure.md

Usage:
    python tools/revive_grand_leisure.py --dry-run   # print the plan, change nothing
    python tools/revive_grand_leisure.py
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

load_dotenv(encoding="utf-8", override=True)

from tools import ebay_sync  # noqa: E402
from tools.ebay_sync import fetch_active_listings, set_quantity  # noqa: E402
from tools.sheet_writer import (append_row, get_sheets_service, read_sheet,  # noqa: E402
                                safe_write_row)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PRODUCTS = [
    {"name": "city heights",
     "title": "Grand Leisure City Heights 6-piece Outdoor Patio Seating Set",
     "costco_url": "https://www.costco.com/.product.100404264.html",
     "sku": "100404264", "qty": 5},
    {"name": "las palmas",
     "title": "Grand Leisure Las Palmas 6-piece Outdoor Patio Seating Set",
     "costco_url": "https://www.costco.com/.product.100404055.html",
     "sku": "100404055", "qty": 5},
]
CATEGORY = "Outdoor Furniture"


def _load_yaml(name):
    with open(os.path.join(_ROOT, "config", name)) as f:
        return yaml.safe_load(f)


def _col_idx(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def match_listings(listings, name):
    """Every live listing whose title contains `name` (case-insensitive)."""
    return [l for l in listings if name.lower() in (l.get("title") or "").lower()]


def sheet_rows_with_title(rows, title_idx, title, start_row):
    """Absolute sheet rows whose col C is exactly `title` (whitespace-stripped)."""
    return [start_row + i for i, r in enumerate(rows)
            if len(r) > title_idx and str(r[title_idx]).strip() == title]


def row_pairs(COL, product, listing):
    """(col, value) pairs for the ACTIVE row — the only columns this script writes."""
    return [
        (COL["status"], "ACTIVE"),
        (COL["title"], product["title"]),
        (COL["category"], CATEGORY),
        (COL["ebay_price"], listing.get("price")),
        (COL["ebay_listing_url"], listing["item_id"]),
        (COL["costco_url"], product["costco_url"]),
        (COL["sku"], product["sku"]),
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    COL = _load_yaml("col_map.yaml")["columns"]
    business = _load_yaml("categories.yaml")["business"]
    sheet_name, start, end = business["sheet_name"], business["data_start_row"], business["data_end_row"]

    listings = fetch_active_listings()
    if not listings:
        print(f"❌ Couldn't fetch eBay listings ({ebay_sync.last_error.get('message') or 'none returned'}) — nothing changed.")
        return 1
    service = get_sheets_service()
    rows = read_sheet(service, f"'{sheet_name}'!A{start}:AA{end}")
    title_idx = _col_idx(COL["title"])

    prefix = "[DRY RUN] " if args.dry_run else ""
    summary, failed = [], False
    for product in PRODUCTS:
        matches = match_listings(listings, product["name"])
        if not matches:
            print(f"{prefix}{product['title']}: not found on eBay, skipped")
            summary.append(f"{product['name']}: not found, skipped")
            continue
        if len(matches) > 1:
            print(f"{prefix}{product['title']}: {len(matches)} listings match — SKIPPED:")
            for m in matches:
                print(f"    {m['item_id']}  qty {m.get('quantity')}  ${m.get('price')}  {m.get('title')}")
            summary.append(f"{product['name']}: ambiguous ({len(matches)} matches), skipped")
            continue
        listing = matches[0]
        item_id, old_qty = listing["item_id"], listing.get("quantity")
        existing = sheet_rows_with_title(rows, title_idx, product["title"], start)
        if len(existing) > 1:
            print(f"{prefix}{product['title']}: title is in sheet rows {existing} — SKIPPED (fix the duplicate)")
            summary.append(f"{product['name']}: duplicate sheet rows {existing}, skipped")
            continue
        pairs = row_pairs(COL, product, listing)
        where = f"update row {existing[0]}" if existing else "append a new row"
        print(f"{prefix}{product['title']}")
        print(f"    eBay {item_id}: '{listing.get('title')}' ${listing.get('price')} "
              f"qty {old_qty} → {product['qty']}")
        print(f"    sheet: {where}: " + ", ".join(f"{c}={v}" for c, v in pairs))
        if args.dry_run:
            summary.append(f"{product['name']}: would set {item_id} qty {old_qty} → {product['qty']}, {where}")
            continue

        res = set_quantity(item_id, product["qty"])
        if not res["ok"]:
            failed = True
            print(f"    ❌ set_quantity failed ({res['error_kind']} {res['error_code']}: {res['message']}) "
                  "— sheet NOT written")
            summary.append(f"{product['name']}: eBay {item_id} quantity FAILED, sheet untouched")
            continue
        print(f"    ✓ eBay {item_id}: qty {old_qty} → {product['qty']}")
        if existing:
            row = existing[0]
            safe_write_row(service, sheet_name, row, pairs)
        else:
            row = append_row(service, sheet_name, dict(pairs), COL, data_start_row=start)
            rows.append([""] * title_idx + [product["title"]])   # a re-run in-process sees it
        print(f"    ✓ sheet row {row} written (ACTIVE)")
        summary.append(f"{product['name']}: {item_id} qty {old_qty} → {product['qty']}, sheet row {row}")

    print("\nSummary:")
    for line in summary:
        print(f"  - {line}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
