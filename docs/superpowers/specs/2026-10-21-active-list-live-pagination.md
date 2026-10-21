# Active Listings: show every live listing + paginate (Oct 21 2026)

## Goal

Jay's Active Listings screen shows only `status == "ACTIVE"` rows, so his non-ACTIVE live
listings (AUDIT_REVIEW / PAUSED_MARGIN / WATCH, etc.) are invisible — he sees ~36 and thinks the
screen "caps" there while ~57 listings are actually live on eBay. Fix two things:

1. **Show every live listing**, not just ACTIVE — same `is_live_listing` test the monitor uses
   (a live eBay listing is live regardless of its sheet status; status is a workflow label only).
2. **Paginate** so the list scales as he grows (Telegram hard-caps an inline keyboard at 100
   buttons, and a 50+ item single list is unwieldy).

## Rule

- "Active Listings" = every row with a live eBay listing (`is_live_listing`: col Q item id, and
  status not in `NOT_LIVE_STATUSES` — ENDED/PAUSED_OOS are hidden/ended and stay out).
- Page size **30** per page, global numbering (page 2 starts at 31), newest sort unchanged
  (`_active_net_key`, net desc).
- The summary header stays aggregated over the FULL live set, not the page slice.

## Changes — `agents/telegram_bot.py`, `cb_activelist_show`

1. Add a module-level constant `_ACTIVE_LIST_PAGE = 30`.
2. Extend the extracted fields so the filter can run: use
   `_ACTIVE_LISTING_FIELDS + ("ebay_listing_url",)` (col Q is needed by `is_live_listing`;
   `status` is already in `_ACTIVE_LISTING_FIELDS`). Keep the `tuple(f for f in … if f in col_map)`
   guard.
3. Drop the `filter_fn=is_active` status filter; extract all rows, then filter the dicts exactly
   like `_active_stepper_order` already does, but with the live-listing test:
   `items = [p for p in items if is_live_listing(p.get("ebay_listing_url"), p.get("status"))]`.
   Sort by `_active_net_key`.
4. Parse the page from `arg` (`arg` may be None for the existing `activelist:show` button):
   `page = max(1, int(arg)) if (arg or "").strip().isdigit() else 1`.
5. Compute `total_pages = max(1, -(-len(items) // _ACTIVE_LIST_PAGE))`, clamp
   `page = min(page, total_pages)`, slice
   `items[(page-1)*_ACTIVE_LIST_PAGE : page*_ACTIVE_LIST_PAGE]`.
6. Number buttons globally: `enumerate(page_items, start=(page-1)*_ACTIVE_LIST_PAGE + 1)`.
7. Summary text: build from the FULL `items` (before slicing); append a `Page X of Y` line (or
   fold it into the header) so Jay knows there are more.
8. Keyboard footer:
   - Prev (`activelist:show:<page-1>`) only when `page > 1`; Next (`activelist:show:<page+1>`)
     only when `page < total_pages`. Use `⬅️` / `➡️` labels.
   - `🔄 Refresh` → `activelist:show:<page>` (reload the current page).
   - `📊 Dashboard` unchanged.
   - The empty-list message ("No active listings right now.") is unchanged and still fires only
     when there are zero live listings.

No new callback route needed — `("activelist", "show")` already routes to `cb_activelist_show`;
it just starts honoring `arg` as the page.

## Tests (`tests/test_telegram_bot.py` or `tests/test_active_list_paging.py`)

- A `PAUSED_MARGIN` / `AUDIT_REVIEW` row with a col Q item id now appears in the list (the old
  code excluded it); an `ENDED` / `PAUSED_OOS` row and a blank-col-Q row stay out.
- More than 30 live listings → page 1 shows exactly 30 (numbered 1..30), page 2 shows the rest
  (numbered 31..), footer shows Prev/Next only where valid.
- `activelist:show` (no arg) = page 1; `activelist:show:2` = page 2; a garbage/out-of-range arg
  clamps safely.
- Summary still reflects the total live count, not the page slice.

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: Active Listings shows every live listing and paginates (30/page)`.
- Push to `master`, reply `DONE — pushed`.
