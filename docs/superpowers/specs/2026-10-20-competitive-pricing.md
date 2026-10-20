# Competitive Pricing — one-tap "be the cheapest" (Oct 20 2026)

## Goal

Add a "⚡ Competitive" action so Jay can list (or reprice) an item at **$0.01 under
the cheapest live competitor**, never below break-even, while he swipes the Review
queue — and on ACTIVE listings.

## Rule (Jay-confirmed Oct 20)

- `L` = lowest live competitor price (newly captured at research time).
- `B` = break-even price (net = $0), rounded UP to .99.
- `target = max(B, L - 0.01)` — undercut the cheapest by a cent, but never list below break-even.
- If `target` is more than **$1.50 above** `L` (break-even floor forces him above the cheapest),
  show a warning with the gap — but still allow listing (he's above break-even).
- **Manual-cost guard:** write col `BE` = col `G` on every competitive price, so the existing
  break-even auto-reprice (`ebay_sync.auto_reprice_breaches`) never overrides it. Re-arms only
  when Costco's cost moves — same semantics as the manual "Set Price" flow.

## Changes

### 1. `tools/ebay_research.py` — capture the true lowest

- `_build_search_urls`: add `&_sop=15` to the **active** search URL (sort: price + shipping,
  lowest first) so page 1 reliably contains the cheapest listing. Leave the sold URL unchanged.
  NOTE: this changes *which* active listings are scraped (cheapest-first instead of best-match),
  so `median_active`/`active_count` may shift slightly — that's expected and fine.
- Add `"lowest_active": None` to the initial `result` dict in `get_ebay_comps`.
- In `_run_query`, after `active_count, active_prices_raw = _scrape_ebay_page(...)`, set
  `result["lowest_active"] = round(min(active_prices_raw), 2)` when `active_prices_raw` is
  non-empty, else leave it `None`. (`active_prices_raw` is already junk-title-filtered and `> $1`;
  do NOT run it through `_filter_outliers` — that trims the bottom 10% and would hide the real
  cheapest competitor.)
- Keep `median_active` from the trimmed list exactly as today.

### 2. New column `BF` = `lowest_active`

- `config/col_map.yaml`: under hidden cols (after `manual_cost: "BE"`), add `lowest_active: "BF"`.
- `tools/sheet_formatter.py`:
  - add to `HEADER_LABELS`: `"LOWEST ACTIVE",  # BF 57 — cheapest live competitor price (research); competitive-pricing floor`
  - bump `TOTAL_COLS = 57` → `58`.
- `tools/sheet_writer.py` `required_grid_columns()` auto-derives from col_map (rightmost col + 1),
  so no change needed — but confirm it now returns 58.

### 3. `agents/researcher.py` — persist `lowest_active`

- Where the research pipeline writes the comp fields (`sold_90d` K, `avg_price` L, `comp_count` M),
  also write `lowest_active` (col BF) when `ebay_data.get("lowest_active")` is not None.
- Add a `Lowest live competitor: $X` line to the `full_notes` (col AV) narrative for human context.

### 4. `tools/reprice.py` — break-even + competitive-target helpers

Add two functions, reusing the already-imported `round_up_99`, `compute_net`,
`COSTCO_TAX_RATE`, `EBAY_FEE_FLAT`, `EBAY_FEE_TAX_FACTOR`:

```python
def break_even_price(cost, fee_rate, ship=0.0, ad_rate=0.0):
    """Price at which net = 0, rounded UP to .99. None if unusable input or fee+ad >= 100%."""
    # denom = 1 - fee_rate*EBAY_FEE_TAX_FACTOR - ad_rate ; if denom <= 0 return None
    # raw = (cost*(1+COSTCO_TAX_RATE) + EBAY_FEE_FLAT + ship) / denom
    # return round_up_99(raw) ; None when cost/fee_rate unparseable or cost <= 0

def competitive_target(lowest_active, cost, fee_rate, ship=0.0, ad_rate=0.0):
    """-> dict {lowest, break_even, target, gap, above_lowest, note}. target None when unusable."""
    # lowest_active None -> {..., "target": None, "note": "no live competitors scraped"}
    # undercut = round(lowest_active - 0.01, 2)
    # B = break_even_price(...) ; None -> {"target": None, "note": "can't compute break-even"}
    # target = max(B, undercut)
    # gap = round(target - lowest_active, 2)   (<=0 undercut/match; >0 above cheapest)
    # above_lowest = gap > 0
    # note = f"{gap:.2f} above the cheapest (break-even floor)" when gap > 0 else ""
```

### 5. `agents/telegram_bot.py` — button + handlers

Review queue:
- Add `"lowest_active"` to `_LOOKUP_PRODUCT_FIELDS`; also add `"comp_count"` and `"avg_price"`
  (cols M/L) for the comps line.
- In `format_product_detail`, append a compact comps line when any piece exists:
  `Comps: cheapest $X · N live · avg sold $Y` (render only present pieces; skip entirely if all
  blank). Update any test asserting the exact card body.
- `_review_card_kb(row_num)`: add a `⚡ Competitive` button (callback `review:compete:<row_num>`).
- `cb_review_compete(update, context, arg)`: resolve the item via the active queue; compute
  `competitive_target(lowest_active, costco_cost, fee_rate, ship_cost)` (ad_rate 0). If
  `target` is None, show the reason and return. Else show a preview: `List $X.XX (cheapest $L, net $N)`,
  add `⚠️ $G.XX above the cheapest — still list?` when `gap > 1.50`, then buttons
  `✓ List at $X.XX` (callback `review:compete_go:<row_num>:<cents>`) and `Cancel`.
- `cb_review_compete_go(update, context, arg)`: parse `row_num:cents` → write `ebay_price` (H) =
  target, `manual_cost` (BE) = current costco_cost (G), `status` = APPROVED; increment the
  approved tally and advance the queue (mirror `cb_review_approve`).

ACTIVE listings:
- `_active_action_kb`: add `⚡ Competitive` (callback `reprice:compete:<item_id>`) whenever
  `item_id` is present.
- `cb_reprice_compete(update, context, arg)`: resolve the ACTIVE row (reuse `_price_row_for_item`
  / `_rows_for_item_id`), compute `competitive_target`, show the same preview, and on confirm call
  `_reprice_apply(item_id, target, manual=True)` — which already writes col H + col BE, revises
  eBay, and clears col P.

### 6. Tests

- `tests/test_reprice.py`: `break_even_price` (happy path, fee+ad≥100% → None) and
  `competitive_target` (undercut happy path, break-even floor, no-lowest → None, gap>0 note).
- `tests/test_ebay_research.py`: `lowest_active` = min of raw active prices; None when empty.
- `tests/test_telegram_bot.py`: `cb_review_compete_go` writes H/BE/APPROVED; `format_product_detail`
  comps line renders only present pieces.
- Header drift: update any `TOTAL_COLS == 57` assertion to `58`.

## Acceptance

- Run the curated safe-suite invocation (NOT bare `pytest tests/ -q` — it hangs on Chrome/API
  imports). ~40 passing; ignore the known unrelated
  `test_menu.py::test_get_all_items_each_has_required_keys` failure.
- Commit message: `feat: one-tap competitive pricing (undercut lowest, break-even floor)`.
- Push to `master`, reply `DONE — pushed`.
