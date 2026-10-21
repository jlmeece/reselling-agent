# Finish eBay → sheet sync: eBay-side SKUs + missing Costco images (Oct 21 2026)

## Goal

The sheet is the source of truth. eBay is what's out of sync, and this closes both gaps:

- **SKUs:** the sheet's col AA holds the real SKU (Costco product ID). The live eBay listings
  still carry old `ROWnn` SKUs. Update each listing's SKU on eBay to match col AA.
- **Images:** ~28 of the ~56 live listings have an EMPTY image column (col AT), so the photo
  pipeline has nothing to composite a branded main photo from. Scrape Costco for those products
  and fill col AT, so every listing can get its branded photo.

## Part A — eBay-side SKU sync (API, no Chrome)

1. `tools/ebay_sync.py`: add `revise_sku(item_id, sku)` — a ReviseFixedPriceItem body that sets
   `Item.SKU`. Mirror the existing `revise_description` / `revise_fixed_price` plumbing.
2. `tools/sync_ebay_skus.py` (one-off, `--dry-run`, idempotent):
   - `fetch_active_listings()` → item id + title.
   - Match each to its sheet row via col Q (eBay item id); read col AA (SKU).
   - If the listing's current SKU differs from col AA, revise it.
   - Skip listings with no col Q match, blank col AA, or non-fixed-price (reuse the guard).
   - `--dry-run` prints `item_id · title · ROWnn → <costco-id>` for every change.

## Part B — Backfill missing Costco images (Chrome, Windows-only)

1. `tools/backfill_images.py` (one-off, `--dry-run`):
   - Read live listings (ACTIVE, has eBay URL) whose col AT is empty.
   - For each, scrape Costco (col R `costco_url`) for the product's image URLs — reuse the
     image-extraction chain already in `tools/costco_scraper.py` (CDN hosts / `Enlarge Product
     Preview` / `og:image`).
   - Write the image URLs to col AT (comma- or pipe-separated, matching what the compositor
     parses). Do NOT touch any other column.
   - Skip + flag products where Costco returns 404 / OOS / no images (report them; they'll need
     a manual photo).
   - `--dry-run` prints `item_id · title · would-write N image URLs` (or `skip: 404/no-images`).

## Order of operations (do after both parts are reviewed)

1. Part A (SKU sync) and Part B (image backfill) each run `--dry-run` first; paste both plans.
2. After approval, run both for real.
3. Then run `python tools/backfill_active_photos.py` so every listing (now with a SKU + images)
   gets its branded main photo.

## Pitfalls

- Never re-list — revise in place, keep rank/watchers/sold.
- Part A is API-only; Part B needs the real Chrome Costco session (local only).
- Blank col AT is the trigger for Part B; don't overwrite a listing that already has images.

## Acceptance

- Commit `feat: sync eBay SKUs to sheet + backfill missing Costco images`, push to `master`,
  reply `DONE — pushed` with per-part counts (SKUs changed, images backfilled, skipped+why).
