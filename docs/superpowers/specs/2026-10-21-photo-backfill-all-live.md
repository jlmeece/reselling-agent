# Extend photo backfill to ALL live listings, not just ACTIVE (Oct 21 2026)

## Goal

`tools/backfill_active_photos.py` filters on `status == "ACTIVE"`, so it only stamped 33 of the
~57 live eBay listings. The other ~24 are in non-ACTIVE sheet statuses (PAUSED_OOS, PAUSED_MARGIN,
AUDIT_REVIEW, WATCH, …) but still have a live eBay listing in col Q. They need the branded main
photo too. Make the target set "every live eBay listing", matching the description scrub (which
covered all 57).

## Change

In `tools/backfill_active_photos.py` `build_targets()` (line ~54):

- Replace the `job["status"] != "ACTIVE"` filter with **"has an eBay item id in col Q"** — the
  same live-listing test the description scrub uses.
- Keep the eBay-side guard that skips a listing whose `listing_status != "Active"` (hidden/ended
  listings must not be revised).
- Keep the existing skip for: no col AT image URLs, already-branded main photo, non-fixed-price.

## Acceptance

- Update the tests to cover a non-ACTIVE row that has a col Q item id (it should be included) and
  a row with no col Q (still skipped).
- Run `--dry-run` and paste the new target count (should be ~57 minus already-branded, not 33).
- Then run for real.
- Commit `fix: photo backfill targets all live listings, not just ACTIVE`, push to `master`,
  reply `DONE — pushed` with the count revised.
