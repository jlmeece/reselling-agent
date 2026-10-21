# Final polish — monitor every LIVE listing, not just ACTIVE (Oct 21 2026)

## Goal

A sheet status must NEVER cause a live eBay listing to go unmonitored. Today `ACTIVE_MONITOR_STATUSES
= {"ACTIVE"}` means only ACTIVE rows get full monitoring (price-change detection, margin check,
sale-end handling). Non-ACTIVE rows with a live eBay listing (PAUSED_MARGIN, PAUSED_OOS,
AUDIT_REVIEW, WATCH) get only a stock-check pass — so they can sell at a loss, miss a Costco price
rise, or ride through a sale-end with nothing catching it. Fix the root cause: key monitoring on
**"has a live eBay listing"** (col Q), not on sheet status.

## The one rule

**A row with a live eBay listing (col Q parseable item id, eBay `listing_status == "Active"`) is
fully monitored — stock, price, margin, sale-end — regardless of sheet status.** The sheet status
keeps driving the WORKFLOW (research/approval pipeline), but never the PROTECTION of a live listing.
If Jay doesn't want a listing to sell, he ENDS it on eBay; the sheet status alone must not un-monitor it.

## Change

1. **One helper, used everywhere.** Add `is_live_listing(row)` (col Q has an item id; reuse the
   existing `_live_listing` / `extract_item_id` test) and route all monitoring gates through it.

2. **Active monitor** (`agents/scheduler.py` `run_active_monitor`): stop the "stock-check-only for
   non-ACTIVE live listings" downgrade. Every live-listing row gets the full pass — stock check,
   Costco price/cost re-scrape, margin calc, sale-expiry check, and the reprice prompt. Keep the
   existing status-transition rules (OOS → hide + PAUSED_OOS; restock → restore prior status), but
   do NOT use sheet status to decide *whether* to monitor.

3. **ebay_sync break-even auto-reprice**: already specced separately
   (`2026-10-21-breakeven-reprice-all-live.md`) — every live listing with net < 0 gets repriced to
   break-even. Fold that in if it hasn't landed yet; do not double-spec.

4. **Sale-end handling** (`SALE_EXPIRY_STATUSES`): replace the status set with the live-listing
   test. A live listing's sale-end is tracked regardless of sheet status.

5. **Alerts**: margin-breach / price-change / sale-end alerts fire for every live listing, not just
   ACTIVE.

## Explicitly NOT changing

- Workflow statuses (PENDING → SCORED → APPROVED → READY → ACTIVE) still drive the research and
  approval pipeline exactly as today.
- `PAUSED_DEMAND` / `PAUSED_SEASONAL` rows WITHOUT a live listing stay untouched.
- A manually-priced row (col BE == col G) still skips auto-reprice.
- Never enforce a target margin — only fix losing (net < 0) listings to break-even.

## Tests

- A PAUSED_MARGIN row with a live listing now gets a Costco price-change prompt + margin calc + sale-end check.
- An AUDIT_REVIEW / WATCH row with a live listing gets the same full monitoring.
- A PAUSED_DEMAND row with NO live listing is untouched.
- A row with a live listing whose eBay `listing_status != "Active"` is skipped (not revised).

## Acceptance

- Run the FULL curated suite (this touches the monitor core — be thorough).
- Commit `fix: monitor every live listing regardless of sheet status`, push to `master`, reply
  `DONE — pushed` with a list of every code path changed and the test count.
