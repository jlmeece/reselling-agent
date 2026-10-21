# Un-gate the reprice buttons — work on any live listing, not just ACTIVE (Oct 21 2026)

## Goal

Jay's Energy Shot is live on eBay (still $41.48) but its sheet row is **AUDIT_REVIEW**, so the
reprice buttons refused the tap before it ever reached eBay. The executor already handles any
live listing (the 2026-10-21 monitor-every-live-listing fix), but the button gate was never
updated to match — so a live AUDIT_REVIEW / PAUSED_MARGIN listing can be auto-repriced by
`ebay_sync` yet cannot be repriced by Jay's tap. Fix the gate, not the executor.

## Root cause

`_price_row_for_item` in `agents/telegram_bot.py` still does
`if (p.get("status") or "").strip() != "ACTIVE": return None, "this listing is no longer ACTIVE …"`.
Every reprice entry point funnels through it — 🎚️ Adjust (`_stepper_row`), ⚡ Competitive
(`cb_reprice_compete`), and 💲 Set Price (`cb_reprice_set`) — so a live non-ACTIVE row is refused
up front. Meanwhile `_reprice_apply` (the executor) already accepts any live status via
`is_live_listing(...)` and lets its `GetItem` check refuse anything not actually Active on eBay.

## Rule

Sheet status is a **workflow label**, never a reason to block a price change on a listing that is
live on eBay. Use the same test the executor uses: `is_live_listing`.

## Changes

### 1. `agents/telegram_bot.py` — `_price_row_for_item`

- Replace the ACTIVE-only guard with the live-listing test:

```python
if not is_live_listing(p.get("ebay_listing_url"), p.get("status")):
    return None, f"this listing isn't live on eBay (status {p.get('status') or 'blank'})."
```

- Update the docstring: "unique ACTIVE row" → "unique row whose listing is live on eBay".
- `is_live_listing` is already imported (used by `_reprice_apply`) — no import change.
- Do NOT touch `_reprice_apply` — it already does the same test plus the `GetItem`
  listing-status guard, so the executor stays the authoritative double-check.

### 2. Tests

- `tests/test_price_stepper.py::test_nudge_refuses_non_active_row`: it uses status `ENDED`, which
  `is_live_listing` still treats as not-live (`NOT_LIVE_STATUSES`). Update the asserted message
  from `"no longer ACTIVE"` to `"isn't live on eBay"`.
- Add a regression test: an `AUDIT_REVIEW` row with a parseable col Q now PASSES the gate (the
  stepper opens / `_price_row_for_item` returns the row, not an error). Assert a `PAUSED_MARGIN`
  live row is also accepted, and that `ENDED` and `PAUSED_OOS` are still refused.
- Search `tests/` for any other assertion on the old `"no longer ACTIVE"` refusal (e.g. in
  `test_reprice_bot.py`) and update them to the new message/behavior — a live non-ACTIVE row must
  now be accepted, only `ENDED`/`PAUSED_OOS` refuse.

## Acceptance

- Run the curated safe suite. Commit message:
  `fix: reprice buttons work on any live listing, not just ACTIVE (AUDIT_REVIEW/PAUSED tap)`.
- Push to `master`, reply `DONE — pushed`.
