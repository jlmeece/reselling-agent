# Cost basis uses current Costco price (col G), not stale buy_cost (Oct 21 2026)

## Goal

The detail card and the price stepper disagree on net for the same live listing
(e.g. Gourmia oven: card `Net $55.75`, stepper `Net $12.45`). Root cause: the card's net
(col I formula `=H-G-AC-AD-AE-AF`) uses **col G `costco_cost`** (today's Costco price), while
the stepper's net + break-even go through `_cost_basis`, which **prefers col BB `buy_cost`** —
a stale snapshot holding the pre-sale regular price ($159.99 vs the $119.99 sale price).

Jay drop-ships, so his real cost is whatever Costco charges at the moment of sale = current
col G. Flip the cost-basis priority so everything keys off col G, making the card, stepper,
break-even, competitive target, and the auto-reprice safety net all agree.

## Rule

- `_cost_basis` returns col G `costco_cost` when it parses to a positive number; it falls back
  to col BB `buy_cost` only when G is blank / unparseable / ≤ 0.
- No caller changes: stepper `_price_metrics`/`_stepper_break_even`, `_competitive_for`,
  `margin_flag`, auto-reprice `_break_even`, `apply_target_prices`, and the scheduler all already
  pass through `_cost_basis`, so they inherit the fix.
- The `buy_cost` auto-snapshot (ebay_sync — "snapshot G into BB when BB is blank on a new sale")
  is left untouched: with G now preferred, BB is only ever a fallback value.

## Changes

### 1. `tools/ebay_sync.py` — `_cost_basis` (flip priority)

Replace the body + docstring:

```python
def _cost_basis(buy_cost, costco_cost):
    """buy_cost (what Jay actually paid) if present, parseable and > 0, else costco_cost."""
    bc = _to_float(buy_cost)
    if bc is not None and bc > 0:
        return bc
    return _to_float(costco_cost)
```

with:

```python
def _cost_basis(buy_cost, costco_cost):
    """costco_cost (col G — current Costco price, what a drop-shipper pays at sale time)
    if present, parseable and > 0, else buy_cost (col BB legacy snapshot) as fallback."""
    g = _to_float(costco_cost)
    if g is not None and g > 0:
        return g
    return _to_float(buy_cost)
```

### 2. `tools/ebay_sync.py` — module docstring accuracy

Update the module-docstring lines that still describe buy-cost-first semantics (the
`cost_basis = buy_cost (col BB, manual) if ... else col G` note and the `costco_cost → buy_cost`
snapshot note) to say col G is preferred and col BB is the fallback.

### 3. `config/col_map.yaml` + `CLAUDE.md` — comment accuracy

- `col_map.yaml` `buy_cost` (BB): note it is now only a fallback (col G wins).
- `CLAUDE.md` `config/col_map.yaml` bullet (`BB buy_cost is manual`): adjust to reflect the
  new precedence.

## Tests

- `test_ebay_sync.py::test_cost_basis_prefers_buy_cost_else_costco` — rename to
  `test_cost_basis_prefers_costco_else_buy_cost` and flip the first assertion:
  `cb("$45.00", "$60.00") == 60.0` (col G wins). The blank / `"junk"` / `"0"` buy_cost cases and
  the `cb("", "") is None` case stay the same. Add `cb("$20.00", "") == 20.0` (fallback to BB
  when G is blank).
- `test_ebay_sync.py::test_buy_cost_overrides_costco_cost` — flip: with `buy_cost="$20"` +
  `costco_cost="$30"` at a $29 live price, the row is now a **hard breach** (cost = $30 → net < 0),
  so `rep["margin_breach"]` has one entry instead of `[]`.
- The existing fallback case (`costco_cost=""`, `buy_cost="$30"`) must still pass unchanged.

## Acceptance

- Run the full test suite. Commit message:
  `fix: cost basis uses current Costco price (col G), not stale buy_cost`.
- Push to `master`, reply `DONE — pushed`.
