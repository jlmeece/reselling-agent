"""
One-off: put the branded main photo on every LIVE eBay listing (any sheet status with an eBay
item id in col Q, like the description scrub) that was created before the photo pipeline existed — by revising the live listing in place (ReviseFixedPriceItem PictureDetails),
never re-listing, so rank / watchers / sold history are kept.

Per row with an eBay item id (col Q), a SKU (col AA) and col AT image URLs, whatever its status:
  1. GetItem: must be Active, FixedPriceItem, not multi-variation; read its current pictures.
     Skipped when its first picture is already the branded photo (idempotent re-runs).
  2. photo_compositor builds data/listing_photos/<sku>.jpg (kept if it already exists).
  3. upload_photos hosts it on eBay (EPS) -> data/hosted_photos.json.
  4. revise_picture(item, branded, existing pictures) — the existing gallery is re-sent after the
     new main photo (eBay replaces the whole set), capped at 24.

--dry-run: GetItem only (read-only) + the plan per item; no photo built, uploaded or revised.
--sku / --limit restrict the run (e.g. one listing as a live trial).

Spec: docs/superpowers/specs/2026-10-21-backfill-active-photos.md

Usage:
    python tools/backfill_active_photos.py --dry-run
    python tools/backfill_active_photos.py --sku X4fb2cfc373      # one listing
    python tools/backfill_active_photos.py
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
            encoding="utf-8", override=True)

from tools import photo_compositor as pc  # noqa: E402
from tools import upload_photos as up  # noqa: E402
from tools.ebay_sync import (MAX_LISTING_PICTURES, extract_item_id, fetch_item_price,  # noqa: E402
                             revise_picture)


def _pic_key(url):
    """Stable identity of an EPS picture: the /z/<id>/ (or /g/<id>/) token; else the URL."""
    m = re.search(r"/[zg]/([^/]+)/", str(url or ""))
    return m.group(1) if m else str(url or "").strip()


def build_targets(rows, start, cols, sku=None):
    """Jobs (photo_compositor shape) + item_id for every row with an eBay item id in col Q — any
    sheet status (PAUSED_OOS / PAUSED_MARGIN / AUDIT_REVIEW / WATCH listings are live too);
    optionally one SKU. Rows without a col Q item id are not targets. Whether the listing is
    still Active on eBay is checked per item by plan_item (GetItem)."""
    q = cols.get("ebay_listing_url")
    if q is None:
        return []
    out = []
    for offset, row in enumerate(rows):
        if not row:
            continue
        item_id = extract_item_id(pc._cell(row, q))
        row_sku = pc._cell(row, cols["sku"])
        if not item_id or (sku and row_sku != sku):
            continue
        out.append({
            "row":     start + offset,
            "status":  pc._cell(row, cols["status"]).upper(),
            "title":   pc._cell(row, cols["title"]),
            "sku":     row_sku,
            "urls":    pc.parse_image_urls(pc._cell(row, cols["image_urls"])),
            "item_id": item_id,
        })
    return out


def plan_item(job, live, hosted_url):
    """-> (action, detail, gallery). action: 'revise' | 'skip'."""
    if not job["sku"]:
        return "skip", "no SKU in col AA", []
    if not job["urls"]:
        return "skip", "no image URL in col AT", []
    if not job.get("item_id"):
        return "skip", "no eBay item id in col Q", []
    if not live["ok"]:
        return "skip", f"GetItem failed ({live['error_code'] or live['error_kind']}: {live['message'][:90]})", []
    if live["listing_status"] and live["listing_status"] != "Active":
        return "skip", f"listing is {live['listing_status']}", []
    if live["listing_type"] != "FixedPriceItem":
        return "skip", f"listing type {live['listing_type'] or 'unknown'} (not FixedPriceItem)", []
    if live["has_variations"]:
        return "skip", "multi-variation listing", []
    existing = live.get("picture_urls") or []
    if hosted_url and existing and _pic_key(existing[0]) == _pic_key(hosted_url):
        return "skip", "already has the branded main photo", []
    keep = hosted_url and _pic_key(hosted_url)
    gallery = [u for u in existing if _pic_key(u) != keep][:MAX_LISTING_PICTURES - 1]
    return "revise", "", gallery


def main(argv=None):
    ap = argparse.ArgumentParser(description="Branded main photo onto every live eBay listing")
    ap.add_argument("--dry-run", action="store_true", help="read-only: print the plan per item")
    ap.add_argument("--sku", help="only this SKU (col AA)")
    ap.add_argument("--limit", type=int, help="revise at most N listings")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    cols = pc._load_columns()
    cols["ebay_listing_url"] = pc._col_idx(_col_letter("ebay_listing_url"))
    rows, start = pc.read_rows()
    targets = build_targets(rows, start, cols, sku=args.sku)
    hosted = up.load_hosted()

    tpl = None
    if not args.dry_run:
        try:
            tpl = pc.load_template()
        except FileNotFoundError as e:
            print(str(e))
            return 1

    prefix = "[DRY RUN] " if args.dry_run else ""
    revised, skipped, failed, n_revise = [], [], [], 0
    print(f"{prefix}{len(targets)} row(s) with an eBay item id in col Q (any status)")
    for job in targets:
        key = pc.safe_sku(job["sku"]) if job["sku"] else ""
        live = fetch_item_price(job["item_id"]) if job.get("item_id") else {"ok": False}
        hosted_url = up.hosted_url_for(key, hosted) if key else None
        action, detail, gallery = plan_item(job, live, hosted_url)
        label = (f"row {job['row']} {job['status'] or '-'} {job['item_id'] or '-'} "
                 f"{job['sku'] or '-'} {job['title'][:40]!r}")
        if action == "skip":
            skipped.append((label, detail))
            print(f"  skip   {label}: {detail}")
            continue
        if args.limit is not None and n_revise >= args.limit:
            skipped.append((label, "over --limit"))
            continue
        n_revise += 1
        photo = pc.OUTPUT_DIR / f"{key}.jpg"
        main_txt = hosted_url or (f"(to upload) {photo.name}" if photo.exists() else f"(to build) {photo.name}")
        print(f"  {'would revise' if args.dry_run else 'revise'} {label}: main {main_txt[:90]} "
              f"+ {len(gallery)} existing picture(s)")
        if args.dry_run:
            continue

        stats = pc.run([job], tpl)                       # keeps an existing jpg
        if stats["failed"] or not photo.exists():
            failed.append((label, f"photo build failed: {stats['failed'] or 'no output'}"))
            continue
        res = up.run(sku=key)
        hosted = up.load_hosted()
        hosted_url = up.hosted_url_for(key, hosted)
        if not hosted_url:
            failed.append((label, f"upload failed: {res['failed']}"))
            continue
        _, _, gallery = plan_item(job, live, hosted_url)
        out = revise_picture(job["item_id"], hosted_url, gallery, listing_type=live["listing_type"])
        if out["ok"]:
            revised.append(label)
            print(f"  ✓ {job['item_id']} → revised ({len(out['pictures'])} pictures)")
        else:
            failed.append((label, f"{out['error_kind']} {out['error_code']}: {out['message'][:120]}"))
            print(f"  ✗ {job['item_id']}: {out['message'][:120]}")
            if out["error_kind"] == "auth":
                print("eBay token rejected — stopping.")
                break

    print(f"\n{prefix}Summary: {'would revise' if args.dry_run else 'revised'} "
          f"{n_revise if args.dry_run else len(revised)} · skipped {len(skipped)} · failed {len(failed)}")
    for label, why in failed:
        print(f"  FAILED {label}: {why}")
    return 1 if failed else 0


def _col_letter(name):
    import yaml
    with open(pc.ROOT / "config" / "col_map.yaml") as f:
        return yaml.safe_load(f)["columns"][name]


if __name__ == "__main__":
    sys.exit(main())
