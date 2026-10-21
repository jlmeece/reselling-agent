"""
One-off: fill col AT (image URLs) for live listings whose col AT is empty, so the branded-photo
pipeline has a source image. Scrapes each row's Costco page (col R) with costco_scraper's image
chain and writes ONLY col AT (" | "-separated, like the active monitor). Never overwrites a
filled col AT. Windows only (real Chrome Costco session).

Targets: ACTIVE rows with an eBay item id in col Q and a blank col AT. Skipped + reported: no
Costco URL, page removed (404/410/not found), CHECK FAILED, out of stock, no images on the page.

Spec: docs/superpowers/specs/2026-10-21-sync-ebay-skus-images.md (Part B)

Usage:
    python tools/backfill_images.py --dry-run   # scrapes (read-only), writes nothing
    python tools/backfill_images.py
"""
import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"), encoding="utf-8", override=True)

from tools.ebay_sync import extract_item_id  # noqa: E402

MAX_IMAGES = 5


def _col(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _cell(r, i):
    return str(r[i]).strip() if i < len(r) else ""


def find_targets(rows, start, COL):
    """ACTIVE rows with an eBay item id (col Q) and an empty col AT -> [{row, item_id, title, url}]."""
    s_i, t_i, q_i = _col(COL["status"]), _col(COL["title"]), _col(COL["ebay_listing_url"])
    r_i, at_i = _col(COL["costco_url"]), _col(COL["image_urls"])
    out = []
    for off, r in enumerate(rows):
        if _cell(r, s_i) != "ACTIVE" or _cell(r, at_i):
            continue
        iid = extract_item_id(_cell(r, q_i))
        if iid:
            out.append({"row": start + off, "item_id": iid, "title": _cell(r, t_i), "url": _cell(r, r_i)})
    return out


def classify(data):
    """Scrape result -> (image_urls, skip_reason). skip_reason None = write."""
    if data.get("removed"):
        return [], "Costco page removed (404/not found)"
    if data.get("stock_status") == "CHECK FAILED":
        return [], "scrape failed (CHECK FAILED)"
    if data.get("in_stock") is False:
        return [], f"out of stock at Costco ({data.get('stock_status') or 'OOS'})"
    urls = [u for u in (data.get("image_urls") or []) if str(u).startswith("http")][:MAX_IMAGES]
    if not urls:
        return [], "no images on the Costco page"
    return urls, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fill empty col AT from Costco for live listings")
    ap.add_argument("--dry-run", action="store_true", help="scrape and print the plan; write nothing")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    with open(os.path.join(_ROOT, "config", "col_map.yaml")) as f:
        COL = yaml.safe_load(f)["columns"]
    with open(os.path.join(_ROOT, "config", "categories.yaml")) as f:
        business = yaml.safe_load(f)["business"]
    from tools.sheet_writer import get_sheets_service, read_sheet, safe_write_row
    service = get_sheets_service()
    sheet_name, start, end = business["sheet_name"], business["data_start_row"], business["data_end_row"]
    rows = read_sheet(service, f"'{sheet_name}'!A{start}:AT{end}")
    targets = find_targets(rows, start, COL)
    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}{len(targets)} live ACTIVE listing(s) with an empty col AT")
    if not targets:
        return 0
    if sys.platform != "win32":
        print("Chrome scrape needs the Windows box — nothing done.")
        return 1

    from tools.costco_scraper import make_browser, scrape_costco
    written, skipped = 0, []
    with make_browser() as page:
        for n, t in enumerate(targets):
            label = f"{t['item_id']} · {t['title'][:50]} (row {t['row']})"
            if not t["url"].startswith("http"):
                skipped.append((label, "no Costco URL in col R"))
                print(f"  skip: {label}: no Costco URL in col R")
                continue
            if n:
                time.sleep(random.uniform(1, 2))
            try:
                data = scrape_costco(t["url"], page=page)
            except Exception as e:
                data = {"stock_status": "CHECK FAILED", "error": str(e)}
            urls, why = classify(data)
            if why:
                skipped.append((label, why))
                print(f"  skip: {label}: {why}")
                continue
            if args.dry_run:
                print(f"  {label} · would-write {len(urls)} image URLs")
                continue
            safe_write_row(service, sheet_name, t["row"], [(COL["image_urls"], " | ".join(urls))])
            written += 1
            print(f"  ✓ {label} · wrote {len(urls)} image URLs to col AT")
    print(f"\n{prefix}Summary: {'would write' if args.dry_run else 'wrote'} "
          f"{len(targets) - len(skipped) if args.dry_run else written} · skipped {len(skipped)}")
    for label, why in skipped:
        print(f"  SKIPPED {label}: {why} — needs a manual photo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
