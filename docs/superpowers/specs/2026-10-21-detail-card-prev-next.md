# Detail card: Prev/Next navigation + unify the live-list order (Oct 21 2026)

## Goal

On the Active Listings detail card there's no way to move to the next item — Jay has to go back
to the list and re-pick. Add `⬅️ Prev` / `Next ➡️` buttons that step through the live listings in
Active List order. While doing so, fix the known mismatch where the stepper's nav order
(`_active_stepper_order`) still filters `status == "ACTIVE"` while the Active List screen walks
every live listing — so prev/next works on AUDIT_REVIEW / PAUSED_MARGIN items too, not just ACTIVE.

## Rule

- One ordered list for everything: the Active List screen, the 🎚 Adjust prev/next, and the detail
  card's new Prev/Next all use `is_live_listing` + `_active_net_key` sort (net desc).
- `⬅️ Prev` / `Next ➡️` appear only when there's an adjacent item; the ends simply omit that button.

## Changes — `agents/telegram_bot.py`

### 1. `_active_stepper_order` — walk every live listing

Replace the ACTIVE-only filter with the same test the Active List screen uses:

```python
items = [p for p in items if is_live_listing(p.get("ebay_listing_url"), p.get("status"))]
```

(dropping the separate `and extract_item_id(...)` — `is_live_listing` already requires a parseable
col Q). Update the docstring to say "live listing" instead of "ACTIVE". This is the one-line fix for
the stepper's prev/next saying "isn't in the ACTIVE list any more" on non-ACTIVE items.

### 2. `_active_action_kb` — accept + render Prev/Next

- Add `prev_row_num=None, next_row_num=None` params.
- Before the existing `⬅️ Active List / 📊 Dashboard` footer, append a nav row:

```python
nav = []
if prev_row_num is not None:
    nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"activelist:pick:{prev_row_num}"))
if next_row_num is not None:
    nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"activelist:pick:{next_row_num}"))
if nav:
    rows.append(nav)
```

### 3. `cb_activelist_pick` — compute + pass Prev/Next

Right after reading the sheet (before the async views fetch), compute the ordered list from the
already-read rows and find this row's neighbours:

```python
order = _active_stepper_order((col_map, service, sheet_name, start, rows))
ids = [r for r, _iid, _t in order]
try:
    i = ids.index(row_num)
except ValueError:
    i = -1
prev_row = ids[i - 1] if i > 0 else None
next_row = ids[i + 1] if 0 <= i < len(ids) - 1 else None
```

Pass `prev_row_num=prev_row, next_row_num=next_row` through to the existing `_active_action_kb(...)`
call.

## Tests

- `_active_stepper_order`: a `PAUSED_MARGIN`/`AUDIT_REVIEW` row with a col Q item id now appears in
  the order; `ENDED`/`PAUSED_OOS`/blank-col-Q stay out (update any test that pinned ACTIVE-only).
- `_active_action_kb`: renders `⬅️ Prev` / `Next ➡️` only when the row numbers are provided; omits
  the row entirely when both are None.
- `cb_activelist_pick`: the keyboard includes `activelist:pick:<prev>` and `activelist:pick:<next>`
  for a middle item, only `Next` for the first, and neither for a lone/absent item.

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: Prev/Next on the detail card (walks every live listing, matches Active List)`.
- Push to `master`, reply `DONE — pushed`.
