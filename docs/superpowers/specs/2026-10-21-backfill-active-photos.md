# Backfill branded photos + SKUs onto existing ACTIVE listings (Oct 21 2026)

## Goal

~30 ACTIVE eBay listings were created before the branded-photo pipeline and SKU backfill existed.
They still show raw Costco images (no JA-LIQUIDATIONS branded main photo) and may have placeholder
`X…` SKUs. Give them the branded main photo + correct SKU **without re-listing** — revise the live
listing in place so rank / watchers / sold history are preserved.

## Why this needs new code (current state)

- `tools/photo_compositor.py` has `ELIGIBLE_STATUSES = {"APPROVED", "READY"}` — it skips ACTIVE rows.
- `tools/ebay_export.py` exports only `READY` rows → the branded photo only flows to NEW listings.
- `tools/ebay_sync.py` has `revise_fixed_price()` (ReviseFixedPriceItem, price only) — there is
  **no picture-revision** path. An existing listing's main image cannot currently be changed.

## Change

### Part 1 — SKU backfill (existing tool, no new code)
`python tools/backfill_skus.py --apply` fills col AA from col R Costco URLs. Run this first.

### Part 2 — photo_compositor handles ACTIVE
Extend `tools/photo_compositor.py` so sheet mode also builds jobs for `ACTIVE` rows (add `"ACTIVE"`
to `ELIGIBLE_STATUSES`, or add an `--include-active` flag). ACTIVE rows with an image URL in col AT
+ a SKU get a branded photo the same as APPROVED/READY do.

### Part 3 — eBay picture revise (tools/ebay_sync.py)
Add `revise_picture(item_id, main_url, gallery_urls)`: a ReviseFixedPriceItem call whose body sets
`Item.PictureDetails.PictureURL` = `[main_url] + gallery_urls`. Mirror the existing
`_build_revise_xml` / `_call` plumbing. The main photo must already be EPS-hosted (upload_photos).

### Part 4 — one-off script tools/backfill_active_photos.py (idempotent, --dry-run)
1. Read ACTIVE rows with a SKU + col AT image URLs.
2. Generate the branded main photo (reuse `photo_compositor.run`).
3. Upload it (reuse `upload_photos`), get the EPS URL.
4. Preserve the listing's existing gallery — fetch current pictures via GetItem and re-send them
   after the new main URL (do NOT blank the gallery).
5. `revise_picture(item_id, branded_main, existing_gallery)`.
6. `--dry-run` prints the full plan (per item: item id, SKU, main URL, gallery count, would-revise).
   Real run executes it and prints `item_id → revised` per success.

## Pitfalls (do not skip)

- **Never re-list.** Use ReviseFixedPriceItem on the existing item id. A re-list loses rank/watchers/sold.
- **Keep the gallery.** Revising PictureURL replaces the set — re-send the existing gallery or the
  listing loses its extra photos.
- **EPS-only.** The branded main must be hosted on eBay (upload_photos), never an external URL.
- **Multi-variation / non-fixed-price** listings: `revise_fixed_price` already guards on
  `listing_type != "FixedPriceItem"` — mirror that guard in `revise_picture` and skip such rows with
  a clear log line.

## Acceptance

- Run `--dry-run` first and paste the plan for Jay to review. Then run for real.
- Commit `feat: backfill branded photos + SKUs onto existing ACTIVE listings`, push to `master`,
  reply `DONE — pushed` with the count revised + which were skipped and why.
