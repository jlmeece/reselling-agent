# Auto-reprice break-even on EVERY live listing, not just ACTIVE (Oct 21 2026)

## Goal

`ebay_sync` auto-reprices losing (net < 0) listings back to break-even, but only for
`status == "ACTIVE"` rows. A listing that's live on eBay but flagged PAUSED_MARGIN (or
AUDIT_REVIEW / WATCH / any non-ACTIVE status) keeps selling at a loss with no auto-fix and no
alert. Extend the break-even auto-reprice to **every row with a live eBay listing**, so no live
listing can lose money silently.

## Change (tools/ebay_sync.py)

1. Find the auto-reprice eligibility check (currently skips `status != "ACTIVE"`). Change it so a
   row is eligible when it has a **live eBay listing** (col Q item id, `listing_status == "Active"`)
   and its computed net profit is **negative**, regardless of sheet status.
2. Keep the existing guard that skips rows Jay priced manually (col BE == col G).
3. Keep the break-even target logic unchanged — only fix *losing* listings to net $0, never push a
   target margin (Jay's rule: "profit is profit", no enforced floor).
4. Non-fixed-price / multi-variation listings: keep the existing skip + log.

## Tests

- A PAUSED_MARGIN row with a live listing and net < 0 is now auto-repriced to break-even.
- A PAUSED_MARGIN row with net >= 0 is left alone (no repricing).
- A row with no live listing (blank col Q) is still skipped.
- A manually-priced row (col BE == col G) is still skipped.

## Acceptance

- Run the curated safe suite. Commit
  `fix: break-even auto-reprice covers all live listings, not just ACTIVE`, push to `master`,
  reply `DONE — pushed`.
