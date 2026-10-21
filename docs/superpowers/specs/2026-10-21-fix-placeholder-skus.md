# Fix placeholder SKUs in col AA → real Costco IDs, then sync (Oct 21 2026)

## Goal

~42 rows have a placeholder SKU in col AA (e.g. `X4fb2cfc373`) — a made-up code `backfill_skus`
wrote before it could parse the Costco URL. The real Costco product ID is available in col R
(`costco_url`) for every one of these rows. Replace the placeholders with the real IDs, keep the
branded photos mapped, then sync eBay so the eBay SKU matches the sheet.

## Change

1. `tools/fix_placeholder_skus.py` (one-off, `--dry-run`, idempotent):
   - Find rows whose col AA is a placeholder (starts with `X` followed by hex, or otherwise not a
     pure digit string — the real Costco ID is digits).
   - Extract the real Costco product ID from col R (`costco_url`) using the existing
     `extract_product_id` / `costco_product_id` helper.
   - Write the real ID to col AA. Do NOT touch any other column.
   - Report `row · X4fb2cfc373 → 4201002163` for every change; skip rows with no parseable col R.
2. **Keep photos mapped:** rename `data/listing_photos/<X…>.jpg` → `<real-id>.jpg` (same `safe_sku`
   rule) and rewrite the matching keys in `data/hosted_photos.json` (both `url` and `gallery`
   entries). Report the renames.
3. Then run `tools/sync_ebay_skus.py` (already built, commit b2431de) so eBay SKUs become the real
   IDs. It reads each listing's current SKU via GetItem, so a re-run is safe.
4. `--dry-run` first for both steps; paste the maps before running for real.

## Pitfalls

- Only rewrite placeholder SKUs — never overwrite a col AA that already holds a real numeric ID.
- The photo rename must use the exact `safe_sku` key the compositor/uploader/export share, or the
  branded photos will orphan.

## Acceptance

- `--dry-run` first (paste the placeholder→real map + photo renames). Then run for real.
- Commit `fix: replace placeholder SKUs with real Costco IDs, then sync eBay`, push to `master`,
  reply `DONE — pushed` with counts (SKUs fixed, photos renamed, eBay SKUs synced).
