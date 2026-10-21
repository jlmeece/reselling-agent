# Stepper step-size tiers + back button on "no comps" (Oct 21 2026)

## Goal

Two UX fixes to the reprice flow:

1. **Bigger nudge steps for high-price items.** The stepper's fixed ±$5/±$1/±$0.25 is useless on a
   $400+ item — Jay taps −$5 dozens of times. Scale the steps to the price.
2. **A back button when "⚡ Competitive" has no comps.** The no-comps message currently dumps to
   Dashboard, forcing Jay to re-navigate the whole menu to get back to the item.

## Rule

- Step sizes scale with the **proposed price** (the value being nudged), two tiers:
  - under **$100**: `−$5 −$1 −$0.25 +$0.25 +$1 +$5` (unchanged)
  - **$100 and up**: `−$50 −$10 −$1 +$1 +$10 +$50`
- "⚡ Competitive" with no computable target shows a **⬅️ Back to item** button returning to the
  item's detail card, so the user is never stranded on the Dashboard.

## Changes — `agents/telegram_bot.py`

### 1. Step tiers

- Keep `_STEP_SIZES` as the small tier. Add a helper:

```python
_STEP_SIZES_BIG = (-5000, -1000, -100, 100, 1000, 5000)   # $50 / $10 / $1

def _step_sizes(cents):
    """Nudge steps for a proposed price: coarse-but-fine for big-ticket items, fine for cheap."""
    return _STEP_SIZES_BIG if cents >= 10000 else _STEP_SIZES
```

- In `_stepper_screen`, build the nudge row from `_step_sizes(cents)` instead of the fixed
  `_STEP_SIZES` (the label is already `f"{'+' if d > 0 else '−'}${abs(d) / 100:g}"`, so −5000 renders
  `−$50` and −10000 would render `−$100`). No change to `cb_reprice_nudge` — it just applies the
  delta (`cents + delta`), which already works for any step.

### 2. Back button on no-comps

- In `cb_reprice_compete`, the `comp["target"] is None` branch (currently
  `reply_markup=_home_inline_kb()`) — replace with a back keyboard using the already-resolved row:

```python
back_kb = InlineKeyboardMarkup([[
    InlineKeyboardButton("⬅️ Back to item", callback_data=f"activelist:pick:{p['row_num']}"),
    InlineKeyboardButton("📊 Dashboard", callback_data="menu:dashboard"),
]])
```

- Leave the earlier `err` path (item not live / no unique col Q) as `_home_inline_kb()` — there's
  no item to go back to.

## Tests

- `_step_sizes`: returns the small tuple for cents < 10000 and the big tuple for cents ≥ 10000.
- `_stepper_screen`: a $49.99 item still renders `−$5/−$1/−$0.25/+$0.25/+$1/+$5` (existing test
  unchanged); a $150 item renders `−$50/−$10/−$1/+$1/+$10/+$50`; a $600 item renders the same big set.
- `cb_reprice_compete` no-comps: emits a `⬅️ Back to item` button with `activelist:pick:<row_num>`
  and no `_home_inline_kb()`-only keyboard.

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: stepper steps scale with price; back button when Competitive has no comps`.
- Push to `master`, reply `DONE — pushed`.
