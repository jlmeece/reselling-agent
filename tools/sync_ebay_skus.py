"""
One-off: make every live eBay listing's SKU match the sheet's col AA (the sheet is the source of
truth; listings still carry old ROWnn SKUs). Revises in place via ReviseFixedPriceItem — never a
re-list. Idempotent: a listing whose SKU already equals col AA is left alone.

Skipped (reported): no sheet row with the item id in col Q, item id in 2+ rows, blank col AA,
non-fixed-price listing.

Spec: docs/superpowers/specs/2026-10-21-sync-ebay-skus-images.md (Part A)

Usage:
    python tools/sync_ebay_skus.py --dry-run
    python tools/sync_ebay_skus.py
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"), encoding="utf-8", override=True)

from tools.ebay_sync import (extract_item_id, fetch_active_listings, fetch_item_price,  # noqa: E402
                             revise_sku)


def with_live_sku(listings, fetch=fetch_item_price):
    """ActiveList sends no SKU (seen live 2026-10-21), so read each listing's current SKU and
    listing type from GetItem. A failed GetItem leaves sku None (= unknown, still compared)."""
    out = []
    for l in listings:
        live = fetch(l["item_id"])
        out.append({**l, "sku": live.get("sku") if live.get("ok") else None,
                    "listing_type": (live.get("listing_type") if live.get("ok") else None)
                    or l.get("listing_type") or ""})
    return out


def _col(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def plan(listings, rows, start, COL):
    """-> (changes, unchanged, skipped). changes = [(listing, row, sheet_sku)]."""
    q_i, aa_i = _col(COL["ebay_listing_url"]), _col(COL["sku"])
    by_item = {}
    for off, r in enumerate(rows):
        iid = extract_item_id(r[q_i] if q_i < len(r) else "")
        if iid:
            by_item.setdefault(iid, []).append((start + off, str(r[aa_i]).strip() if aa_i < len(r) else ""))
    changes, unchanged, skipped = [], [], []
    for l in listings:
        matches = by_item.get(l["item_id"], [])
        if not matches:
            skipped.append((l, "no sheet row with this item id in col Q"))
        elif len(matches) > 1:
            skipped.append((l, f"item id in {len(matches)} sheet rows {[m[0] for m in matches]}"))
        elif not matches[0][1]:
            skipped.append((l, f"blank col AA (row {matches[0][0]})"))
        elif l.get("listing_type") and l["listing_type"] != "FixedPriceItem":
            skipped.append((l, f"listing type {l['listing_type']}"))
        elif (l.get("sku") or "").strip() == matches[0][1]:
            unchanged.append(l)
        else:
            changes.append((l, matches[0][0], matches[0][1]))
    return changes, unchanged, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="Set each live eBay listing's SKU to the sheet's col AA")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, revise nothing")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    listings = fetch_active_listings()
    if not listings:
        print("❌ Couldn't fetch eBay listings — nothing changed.")
        return 1
    with open(os.path.join(_ROOT, "config", "col_map.yaml")) as f:
        COL = yaml.safe_load(f)["columns"]
    with open(os.path.join(_ROOT, "config", "categories.yaml")) as f:
        business = yaml.safe_load(f)["business"]
    from tools.sheet_writer import get_sheets_service, read_sheet
    start, end = business["data_start_row"], business["data_end_row"]
    rows = read_sheet(get_sheets_service(), f"'{business['sheet_name']}'!A{start}:AA{end}")

    changes, unchanged, skipped = plan(with_live_sku(listings), rows, start, COL)
    prefix = "[DRY RUN] " if args.dry_run else ""
    done, failed = 0, []
    for l, row, sku in changes:
        line = f"{l['item_id']} · {(l.get('title') or '')[:55]} · {l.get('sku') or '(none)'} → {sku}"
        if args.dry_run:
            print(f"  {line}  (row {row})")
            continue
        res = revise_sku(l["item_id"], sku, listing_type=l.get("listing_type") or None)
        if res["ok"]:
            done += 1
            print(f"  ✓ {line}")
        else:
            failed.append((l, f"{res['error_kind']} {res['error_code']}: {res['message'][:140]}"))
            print(f"  ✗ {line}: {res['message'][:140]}")
            if res["error_kind"] == "auth":
                print("eBay token rejected — stopping.")
                break
    print(f"\n{prefix}Summary: {'would change' if args.dry_run else 'changed'} "
          f"{len(changes) if args.dry_run else done} · already matching {len(unchanged)} · "
          f"skipped {len(skipped)} · failed {len(failed)}")
    for l, why in skipped:
        print(f"  SKIPPED {l['item_id']} · {(l.get('title') or '')[:55]} (SKU {l.get('sku') or '-'}): {why}")
    for l, why in failed:
        print(f"  FAILED  {l['item_id']} · {(l.get('title') or '')[:55]}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
