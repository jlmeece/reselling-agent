# Sheet-Driven Bulk Reprice — Design Spec
Date: 2026-10-15

## Overview

Let Jay reprice any number of live eBay listings by typing a **target price** in the Google Sheet, then have the tool push those prices to eBay automatically. Introduces a **"manual price wins"** rule so the break-even auto-reprice safety net can never override a price Jay set deliberately (competitive pricing stays put).

## Problem it solves

Today the sheet is write-only for price: col H is the creation price and the record of the last price, but nothing reads a col H edit and pushes it up to eBay. The only ways a live price changes are (1) auto-reprice of losing listings to break-even, (2) a cost-rise tap prompt, (3) a sale-end scheduled action. None read Jay's sheet edits. Bulk reprice via eBay Seller Hub is the only current fast path.

## Design

### Two new columns

| Col | Name | Purpose |
|-----|------|---------|
| BD | `target_price` | Manual target eBay price. A filled cell = explicit instruction to reprice that listing. Cleared after a successful apply. |
| BE | `price_manual_at` | Timestamp set whenever Jay applies a price himself. Signals the auto-reprice safety net to leave this listing alone. |

### The sweep — `apply_target_prices()`

Runs on ACTIVE rows only. For each row:

1. Must have a filled `target_price` (BD) **and** a valid eBay item ID in col Q. Anything else is skipped (non-ACTIVE rows, blank BD, blank/parseable col Q, multi-variation listings).
2. Fetch the live Buy It Now price via `GetItem` (the existing read-only guard — never trust col H, which can be stale).
3. If target == live price → clear BD, set BE, mark no-op.
4. If target != live price → `ReviseFixedPriceItem(target)`. On success: write col H = target, clear BD, set BE = now, log. On failure: leave BD filled so it retries next run, and alert.
5. Below break-even (net < 0 at the target): **warn-and-apply**. Apply his price, but flag it in the result message so he sees it loses money. Never block.
6. No big-jump hold (this is Jay's explicit number, not a guess). A change over ~50% just gets a ⚠️ note in the confirmation as a typo guard.

### Trigger — both scheduled and manual

- **Scheduled:** fold `apply_target_prices()` into the existing `ebay_sync` run (4x/day: 10:10 / 14:10 / 18:10 / 22:10 Central). Filled cells get applied automatically, worst-case ~6h lag.
- **Manual:** a new bot button **"Apply price changes now"** that runs `apply_target_prices()` immediately in the bot process (same pattern as the existing reprice tap, which already calls `revise_fixed_price` in-process). Edit sheet → tap → live in seconds.

Both paths are idempotent: revising to the same price is safe, and BD is cleared after success, so a concurrent second run just finds BD empty.

### Safety net reconciliation (the key rule)

**Manual always wins over automatic.**

- The existing auto-reprice (push net < 0 ACTIVE rows up to break-even) stays exactly as-is **but now skips any row with `price_manual_at` (BE) set.**
- So when Jay deliberately prices a listing below break-even to compete, the target sweep applies it and marks BE, and the safety net will never yank it back up.
- Protection for manually-priced rows still comes from: the cost-rise tap prompt (existing) and the hard-margin alert (existing, 24h dedup). The alert message will note "manually priced" so Jay knows it's his call, not an accident.

### Manual flag lifecycle

`price_manual_at` is **sticky**: set when Jay applies a price himself, cleared only when Jay clears the cell himself (or edits it). The safety net does not re-engage automatically. Rationale: predictable — "once you set a price, that listing is yours; the tool only nudges, never auto-changes it again."

## Files touched

| File | Action | Purpose |
|------|--------|---------|
| `config/col_map.yaml` | Modify | Add `target_price` (BD) and `price_manual_at` (BE) |
| `tools/ebay_sync.py` | Modify | Add `apply_target_prices()`; fold into `run_ebay_sync`; make `auto_reprice_breaches` skip rows with BE set |
| `agents/scheduler.py` | Modify | Wire the sweep into `--mode ebay_sync` (and optionally a standalone `--mode apply_prices` for the manual trigger) |
| `agents/telegram_bot.py` | Modify | Add "Apply price changes now" callback → `apply_target_prices()` |
| `agents/menu.py` | Modify | Add the button to the Operations menu |
| `tests/test_ebay_sync.py` | Modify | New tests for the sweep + manual-override rule |
| `config/categories.yaml` | Modify | Optional small block (below-break-even = warn, default on) |

## Edge cases

- **Non-ACTIVE row with a filled BD** — ignored, noted in the run log. BD only applies to live listings with a col Q item ID.
- **Multi-variation listing** — `ReviseFixedPriceItem` can't set a price without a SKU; skipped and flagged (same limit as existing auto-reprice).
- **BD filled but col Q blank** — skipped; suggests filling col Q (same as existing link-suggestion flow).
- **Below break-even** — warn-and-apply; the hard-margin alert will fire once/day (24h dedup) until Jay changes it.
- **eBay auth error mid-sweep** — stop the loop (every later call fails the same way), leave remaining BD cells filled for retry, alert loudly (same as existing auto-reprice).

## Out of scope

- Re-pricing non-ACTIVE / pre-listing rows from BD (col H remains the creation price).
- Any margin *target* enforcement — the tool only protects break-even and applies what Jay types. Pricing is Jay's call.
- Multi-variation reprice support.
- A "revert to break-even" one-click button (could be added later, not needed now).
