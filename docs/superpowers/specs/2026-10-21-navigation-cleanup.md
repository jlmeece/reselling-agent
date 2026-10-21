# Navigation cleanup — one item card, price editor as a sub-screen (Oct 21 2026)

## Goal

The item has two screens that act like parallel menus (the detail card with views/specs, and the
🎚 price editor), with duplicated buttons and dead-ends. Make the card the single hub and the editor
a sub-screen of it:

- No `⚡ Competitive` on the card (it stays as `⚡ cheapest` inside the editor only).
- The editor gets a `⬅️ Back to card` button and drops its own prev/next.
- After ✓ Apply, offer `Next item →` (the next item's **card**) instead of dumping Jay to the list.

## Rule

- The card is the navigation unit (its Prev/Next — separate spec — walks the live list). The editor
  is a modal on top of it: open from the card, back to the card.
- "Next item" always lands on the next item's **card** (so Jay sees views/specs before deciding to
  reprice), never on the next editor.

## Changes — `agents/telegram_bot.py`

### 1. `_active_action_kb` — drop `⚡ Competitive`

Remove the `⚡ Competitive` button (`reprice:compete:<item_id>`); the card row becomes just
`🎚️ Adjust`. (`cb_reprice_compete` and its route can stay for back-compat with old messages, but no
new card shows the button.)

### 2. `_stepper_screen` — Back to card, no prev/next

- Remove the `← prev` / `next →` nav row (and stop rendering `reprice:nav:…`).
- Add a `⬅️ Back to card` button → `activelist:pick:<row_num>` (the row num is already available to
  `_stepper_screen` via `p["row_num"]`). Keep `⬅️ Active List` as the secondary exit.

### 3. `cb_reprice_stepapply` — after Apply, offer "next item"

When the apply succeeds (the `kb is None` fallback path), instead of `🎚️ Adjust again / ⬅️ Active
List`, build:

```python
order = _active_stepper_order()
ids = [r for r, _iid, _t in order]
try:
    i = ids.index(row_num)          # resolve the current row (via _price_row_for_item, as elsewhere)
except ValueError:
    i = -1
next_row = ids[i + 1] if 0 <= i < len(ids) - 1 else None
```

Buttons: `Next item →` → `activelist:pick:<next_row>` (only when `next_row` is not None),
`⬅️ Back to card` → `activelist:pick:<row_num>`, `⬅️ Active List` → `activelist:show`.

## Tests

- `_active_action_kb`: no `reprice:compete:` in the card's callback data; `🎚️ Adjust` remains.
- `_stepper_screen`: emits `activelist:pick:<row_num>` (Back to card) and no `reprice:nav:` data.
- `cb_reprice_stepapply`: on success, the fallback keyboard has `activelist:pick:<next>` for a middle
  item, no `Next item` for the last item, and `activelist:show` always.

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: card is the nav hub — editor becomes a sub-screen with back + next-item`.
- Push to `master`, reply `DONE — pushed`.
