# Interactive price stepper for ACTIVE listings (Oct 21 2026)

## Goal

Replace the "💲 Set Price" type-a-number flow with a **tap-stepper** on ACTIVE listings.
Jay walks his live listings, sees the cheapest competitor + break-even + live net/margin,
nudges the price up/down by tapping, and applies to eBay — no typing, no math, no sheet.

## Rule (Jay-confirmed Oct 21)

- Telegram has no slider widget, so this is a **tap-stepper**: six nudge buttons
  `−$5 −$1 −$0.25 +$0.25 +$1 +$5`. Each tap edits the same message in place (never floods the chat).
- The screen always shows, as reference notes: the **cheapest competitor** (col BF `lowest_active`),
  **break-even** (net = $0), and the **net + margin** at the proposed price.
- **Below break-even (net < 0)** → red `⚠️ below break-even — this price loses money` line and the
  Apply button relabels to `⚠️ Apply below break-even`. It still applies on tap (deliberate confirm),
  but the loss is impossible to do accidentally. This is the Energy Shot guard.
- **More than $20 over the cheapest** → soft note `>$20 over the cheapest` (his stated ceiling),
  not a block.
- **Tap = preview, Apply = write.** Nudges never write; only `✓ Apply` calls
  `_reprice_apply(item_id, target, manual=True)` (writes col H + col BE = col G, clears col P,
  allows lowering).
- **Prev/next** walk the ACTIVE listings in the existing order (`_active_net_key`, net desc) so he
  can price the whole queue without returning to the list.

## Changes

All in `agents/telegram_bot.py` unless noted.

### 1. Imports

- Add `break_even_price` to the existing `from tools.reprice import (...)` block (line ~58).

### 2. Helpers

```python
_STEP_SIZES = (-500, -100, -25, 25, 100, 500)   # cents

def _parse_step_arg(arg):
    # '<item_id>:<cents>' or '<item_id>:<cents>:<delta>' -> (item_id, cents, delta or None)
    # None on malformed / non-item-id / non-int. Reuse extract_item_id for the id check.

def _stepper_break_even(p):
    # break_even_price(_cost_basis(p.get("buy_cost"), p.get("costco_cost")),
    #                  _to_rate(p.get("fee_rate")), _to_float(p.get("ship_cost")) or 0.0)
    # -> float (rounded) or None

def _active_stepper_order():
    # Ordered [(row_num, item_id, title)] for the stepper: read the sheet, filter status==ACTIVE
    # AND extract_item_id(col Q) non-empty, sort by _active_net_key (same as cb_activelist_show).
    # Returns [] on any sheet-read error (callers treat as "no navigation").

def _stepper_screen(p, cents, item_id):
    # -> (text_html, InlineKeyboardMarkup). Pure, no I/O (caller passes the already-read row p).
    # target = cents / 100. net, margin = _price_metrics(p, target). lowest = _to_float(p.get("lowest_active")).
    # break_even = _stepper_break_even(p). current = _parse_currency(p.get("ebay_price")).
    # Text lines (HTML):
    #   🎚️ <b>Adjust price</b>
    #   <b>{title}</b>
    #   eBay now: ${current}            (only if current parseable)
    #   New price: <b>${target}</b>
    #   ⚡ cheapest competitor: ${lowest}      (only if lowest not None)
    #   🛑 break-even: ${break_even}           (only if break_even not None)
    #   Net: ${net} ({margin}% margin)         (only if net not None)
    #   ⚠️ below break-even — this price loses money     (only when net is not None and net < 0)
    #   >$20 over the cheapest                (only when lowest and target - lowest > 20)
    #   (cost/fee unknown → "⚠️ cost or fee rate missing — margin unknown" in place of the Net line)
    # Buttons:
    #   row1: the six _STEP_SIZES buttons, label f"{'+' if d>0 else '−'}${abs(d)/100:g}",
    #         callback f"reprice:nudge:{item_id}:{cents}:{d}"
    #   row2 (only when a competitive target is computable): "⚡ cheapest: ${comp_target}",
    #         callback f"reprice:cheapest:{item_id}:{cents}"
    #   row3: Apply button (full width). Label "✓ Apply ${target}" when net is None or net >= 0,
    #         else "⚠️ Apply below break-even". callback f"reprice:stepapply:{item_id}:{cents}"
    #   row4: [← prev] f"reprice:nav:{item_id}:prev" · [next →] f"reprice:nav:{item_id}:next"
    #   row5: [⬅️ Active List] callback "activelist:show"
```

For the "⚡ cheapest" quick-set, reuse `_competitive_for(p, _cost_basis(p.get("buy_cost"),
p.get("costco_cost")))` — its `comp["target"]` is `max(break-even rounded to .99, cheapest − $0.01)`,
and is `None` when it can't be computed (no col BF or no cost/fee), which is exactly the
"only when computable" gate for row2.

### 3. Callback handlers (all domain "reprice")

- `cb_reprice_stepper(update, context, arg)` — `arg = item_id`. Validate id; read the sheet
  (`_read_product_rows`); resolve the row via `_price_row_for_item(item_id, rows, col_map, start)`
  (it already refuses non-ACTIVE / 0-or->1 matches). Starting price = `_parse_currency(p.get("ebay_price"))`
  → cents, else `_stepper_break_even(p)` → cents, else 0. Render `_stepper_screen` and
  `edit_message_text` in place (same message).
- `cb_reprice_nudge(update, context, arg)` — `_parse_step_arg(arg)` → (item_id, cents, delta);
  delta must be present. `new_cents = cents + delta` (floor at 1 cent, so price never goes ≤ 0).
  Re-read sheet, re-resolve row (row-shift safe; on 0/≠1 match or not-live, show the refusal text
  and stop), re-render `_stepper_screen` with `new_cents`, `edit_message_text` in place.
- `cb_reprice_cheapest(update, context, arg)` — `_parse_step_arg(arg)` → (item_id, cents, _).
  Re-read + resolve row; `comp = _competitive_for(p, _cost_basis(...))`. If `comp["target"]` is None,
  `answer()` the callback with `_no_competitive_text` and leave the screen unchanged. Else
  `new_cents = round(comp["target"] * 100)`, re-render, `edit_message_text` in place.
- `cb_reprice_stepapply(update, context, arg)` — `_parse_step_arg(arg)` → (item_id, cents, _).
  `target = cents / 100`. Guard double-taps with the existing `context.bot_data["reprice_inflight"]`
  set (same as `cb_reprice_go`). Show `⏳ Repricing eBay item {item_id} to ${target}…`, then
  `await _reprice_apply(item_id, target, manual=True)`, then `edit_message_text` with the result.
- `cb_reprice_nav(update, context, arg)` — `arg = '<item_id>:<prev|next>'`. Build
  `_active_stepper_order()`, find the index of item_id; if not found, `answer()` and stop. Jump to
  index−1 (`prev`) or index+1 (`next`); if the target index is out of range, `answer()` with
  "no more that way" and stop. Resolve the adjacent row via `_price_row_for_item` (it has a live
  item id by construction), start at its col H (or break-even fallback), render `_stepper_screen`,
  `edit_message_text` in place.

Register all five in `_CALLBACK_ROUTES.update({...})`:
`("reprice","stepper")`, `("reprice","nudge")`, `("reprice","cheapest")`, `("reprice","stepapply")`,
`("reprice","nav")`.

### 4. `_active_action_kb` — swap the button

Replace the `💲 Set Price` button (callback `reprice:set:<item_id>`) with
`🎚️ Adjust` (callback `reprice:stepper:<item_id>`). Keep `⚡ Competitive` as-is.

Leave the now-orphaned typing flow (`cb_reprice_set`, `cb_reprice_setcancel`, `_handle_price_input`,
`_price_cancel_kb`, the `awaiting_price` state, and the `reprice:set`/`reprice:setcancel` routes)
in place as a back-compat fallback — old messages with "💲 Set Price" still work. Do NOT delete it;
note in a code comment that it is unreachable from any current button. `_price_confirm_kb` and
`_price_metrics` are still used by `cb_reprice_compete`, so they stay.

### 5. Tests (`tests/test_telegram_bot.py`, or a new `tests/test_price_stepper.py`)

- `_parse_step_arg`: `<id>:<cents>` → (id, cents, None); `<id>:<cents>:<delta>` → (id, cents, delta);
  malformed / non-item-id / non-int → None.
- `_stepper_screen`: renders title + proposed price + cheapest + break-even + net lines only when
  each input is present; emits the `⚠️ below break-even` line and the `⚠️ Apply below break-even`
  label when net < 0; emits the `>$20 over the cheapest` note when `target - lowest > 20`; omits the
  `⚡ cheapest` row when no competitive target.
- `_active_stepper_order`: only ACTIVE rows with a parseable col Q, sorted net-desc.
- `cb_reprice_nudge`: new price = (cents + delta) / 100 (assert via the pure math or a mocked
  `_stepper_screen` call); clamps at $0.01.
- `cb_reprice_stepapply`: calls `_reprice_apply(item_id, target, manual=True)` (patch it, assert args).
- `_active_action_kb`: emits `🎚️ Adjust` with `reprice:stepper:<item_id>` and no `reprice:set`.
- `_CALLBACK_ROUTES` includes the five new `("reprice", …)` keys.

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: tap-stepper to set ACTIVE listing prices (no typing; cheapest + break-even shown)`.
- Push to `master`, reply `DONE — pushed`.
