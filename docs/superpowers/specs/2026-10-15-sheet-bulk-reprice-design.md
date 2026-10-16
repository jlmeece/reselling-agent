# Sheet-Driven Bulk Reprice — Design Spec
Date: 2026-10-15

## Overview

Let Jay reprice any number of live eBay listings by typing a **target price** in the Google Sheet, then have the tool push those prices to eBay automatically. Introduces a **"manual price wins"** rule so the break-even auto-reprice safety net never overrides a price Jay set deliberately — **with automatic re-arm**: once Costco's cost moves away from the cost Jay priced against, the safety net re-engages.

## Problem it solves

Today the sheet is write-only for price: col H is the creation price and the record of the last price, but nothing reads a col H edit and pushes it up to eBay. The only ways a live price changes are (1) auto-reprice of losing listings to break-even, (2) a cost-rise tap prompt, (3) a sale-end scheduled action. None read Jay's sheet edits. Bulk reprice via eBay Seller Hub is the only current fast path.

## Design

### Two new columns

| Col | Name | Purpose |
|-----|------|---------|
| BD | `target_price` | Manual target eBay price. A filled cell = explicit instruction to reprice that listing. Cleared after a successful apply. |
| BE | `manual_cost` | Costco cost (col G) snapshot taken when Jay set a manual price. This is the **re-arm key**: the safety net respects Jay's manual price only while current cost still equals this snapshot. |

### The sweep — `apply_target_prices()`

Runs on ACTIVE rows only. For each row:

1. Must have a filled `target_price` (BD) **and** a valid eBay item ID in col Q. Anything else is skipped (non-ACTIVE rows, blank BD, blank/unparseable col Q, multi-variation listings).
2. Fetch the live Buy It Now price via `GetItem` (the existing read-only guard — never trust col H, which can be stale).
3. If target == live price → clear BD, write BE = current G, mark no-op.
4. If target != live price → `ReviseFixedPriceItem(target)`. On success: write col H = target, clear BD, write BE = current G, log. On failure: leave BD filled so it retries next run, and alert.
5. Below break-even (net < 0 at the target): **warn-and-apply**. Apply his price, but flag it in the result message so he sees it loses money. Never block.
6. No big-jump hold (this is Jay's explicit number, not a guess). A change over ~50% just gets a ⚠️ note in the confirmation as a typo guard.

### Trigger — both scheduled and manual

- **Scheduled:** fold `apply_target_prices()` into the existing `ebay_sync` run (4x/day: 10:10 / 14:10 / 18:10 / 22:10 Central). Filled cells get applied automatically, worst-case ~6h lag.
- **Manual:** a new bot button **"Apply price changes now"** that runs `apply_target_prices()` immediately in the bot process (same pattern as the existing reprice tap, which already calls `revise_fixed_price` in-process). Edit sheet → tap → live in seconds.

Both paths are idempotent: revising to the same price is safe, and BD is cleared after success, so a concurrent second run just finds BD empty.

### Safety net reconciliation — manual wins, then re-arms

**Auto-reprice a net < 0 ACTIVE row to break-even UNLESS it is manually priced against the *current* cost.**

- BE blank → not manually priced → safety net applies (as today).
- BE filled AND current G == BE → Jay priced this listing against this exact cost → safety net skips it (competitive price stays put).
- BE filled AND current G != BE → Costco moved the cost since Jay's manual price → his decision is stale → safety net re-arms and auto-reprices to break-even (and clears BE so the row returns to the unprotected state).

Result: Jay's competitive price is fully respected while the cost environment he priced against is unchanged. The moment Costco raises (or lowers) that item's cost, the safety net re-engages and protects him. A cost *drop* simply makes his manual price more profitable, so no reprice fires (net stays ≥ 0).

### Below break-even

Warn-and-apply. The hard-margin alert (24h dedup) keeps surfacing the loss until Jay changes it, so a deliberate low price is never silent.

## Files touched

| File | Action | Purpose |
|------|--------|---------|
| `config/col_map.yaml` | Modify | Add `target_price` (BD) and `manual_cost` (BE) |
| `tools/ebay_sync.py` | Modify | Add `apply_target_prices()`; fold into `run_ebay_sync`; make `auto_reprice_breaches` skip rows where BE is set AND G == BE |
| `agents/scheduler.py` | Modify | Wire the sweep into `--mode ebay_sync` (and optionally a standalone `--mode apply_prices` for the manual trigger) |
| `agents/telegram_bot.py` | Modify | Add "Apply price changes now" callback → `apply_target_prices()` |
| `agents/menu.py` | Modify | Add the button to the Operations menu |
| `tests/test_ebay_sync.py` | Modify | New tests for the sweep + manual-wins + re-arm rule |
| `config/categories.yaml` | Modify | Optional small block (below-break-even = warn, default on) |

## Edge cases

- **Non-ACTIVE row with a filled BD** — ignored, noted in the run log. BD only applies to live listings with a col Q item ID.
- **Multi-variation listing** — `ReviseFixedPriceItem` can't set a price without a SKU; skipped and flagged (same limit as existing auto-reprice).
- **BD filled but col Q blank** — skipped; suggests filling col Q (same as existing link-suggestion flow).
- **Below break-even** — warn-and-apply; the hard-margin alert fires once/day until Jay changes it.
- **eBay auth error mid-sweep** — stop the loop (every later call fails the same way), leave remaining BD cells filled for retry, alert loudly (same as existing auto-reprice).

## Out of scope

- Re-pricing non-ACTIVE / pre-listing rows from BD (col H remains the creation price).
- Any margin *target* enforcement — the tool only protects break-even and applies what Jay types. Pricing is Jay's call.
- Multi-variation reprice support.
- A "revert to break-even" one-click button (could be added later, not needed now).
