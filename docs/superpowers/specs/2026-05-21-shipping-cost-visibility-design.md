# Shipping Cost Visibility — Design Spec
**Date:** 2026-05-21
**Status:** Approved

## Problem

Col Y currently shows `✓ FREE` when Costco ships for free and is **blank** when shipping has a cost. The actual shipping dollar amount is stored in hidden Col AC (from the cart estimate), and is already factored into the net profit formula (`=H-G-AB-AC-AD`), but there is no visible indicator of what shipping costs or what the all-in delivered price is.

Row 16 and others like it are invisible problems: you see a Costco cost, you see a suggested eBay price, but you cannot tell at a glance what this item actually costs to acquire and ship.

## Goals

1. Col Y shows the actual shipping cost when not free (`$12.99 ship`) — not just blank
2. A new Col Z "TOTAL COST" shows the all-in delivered cost (Costco cost + shipping) as a formula
3. No agent writes required for Col Z — it stays accurate automatically

## Non-Goals

- Changing how net profit or margin is calculated (already correct)
- Adding tax to the total cost column (tax is already handled separately in Col AE/AF)
- Surfacing shipping in the listing copy

---

## Section 1: Col Y — Shipping Badge

**File:** `agents/researcher.py`

**Current logic:**
```python
free_ship_val = "✓ FREE" if free_shipping else ""
```

**New logic:**
```python
if free_shipping:
    free_ship_val = "✓ FREE"
elif cart_est.get("shipping") is not None:
    free_ship_val = f"${cart_est['shipping']:.2f} ship"
else:
    free_ship_val = ""
```

**Conditional formatting** (`tools/sheet_formatter.py`):
- Existing rule: teal background when Y is non-empty → **scope narrowed** to only apply when Y contains "FREE"
- New rule: **amber background** (`#F57C00` / orange-600) when Y contains "ship"
- Bold white text on both

This makes free vs. paid shipping visually distinct at a glance — teal for free, amber for paid.

---

## Section 2: New Col Z — TOTAL COST

**Column definition:**
- Header: `TOTAL COST`
- Letter: `Z`
- Type: Formula (agent never writes here)
- Formula: `=IFERROR(G+AD, G)` — Costco cost + ship_cost (AD is ship_cost after the shift)
- Format: Currency `$0.00`
- Width: 110px
- Position: First hidden column boundary shifts from col 25 → col 26

When shipping is free, ship_cost written to AD is `0.0`, so TOTAL COST equals COST exactly — Col Y's teal FREE badge already communicates why.

When cart estimate failed (ship_cost blank), `IFERROR` falls back to G alone so the cell is never broken.

**`config/col_map.yaml` addition (visible section):**
```yaml
total_cost: "Z"   # formula =IFERROR(G+AD,G) — do not overwrite
```

**`tools/sheet_formatter.py` changes:**
- Add `"TOTAL COST"` to `HEADERS` list at index 25
- `VISIBLE_COLS`: 25 → 26
- `HIDDEN_START`: 25 → 26
- Add column width entry: `25: 110`
- Add formula write for Z during sheet setup
- Update legend tab: add Col Z row to COLUMN QUICK REFERENCE
- Update stats row docstring

---

## Section 3: Hidden Column Shift

Inserting Col Z at position 25 shifts all existing hidden columns one letter to the right.

### Column letter remapping

| Key | Old letter | New letter | Notes |
|-----|-----------|-----------|-------|
| sku | Z | AA | |
| fee_rate | AA | AB | |
| ebay_fees | AB | AC | formula `=H*AA` → `=H*AB` (auto-updated by Sheets) |
| ship_cost | AC | AD | Col Z formula references AD |
| fulfillment | AD | AE | |
| tax_est | AE | AF | formula `=G*0.0825` (unchanged) |
| site_profit | AF | AG | formula `=H*0.90-G` (unchanged) |
| ad_budget | AG | AH | formula `=I*0.15` (unchanged) |
| seo_title | AH | AI | |
| bullets | AI | AJ | |
| description | AJ | AK | |
| redirect_msg | AK | AL | |
| meta_desc | AL | AM | |
| keywords | AM | AN | |
| alt_text | AN | AO | |
| google_hl | AO | AP | |
| google_desc | AP | AQ | |
| meta_text | AQ | AR | |
| meta_hl | AR | AS | |
| image_urls | AS | AT | |
| perf_score | AT | AU | |
| full_notes | AU | AV | |

**Net profit formula** in Col I: was `=H-G-AB-AC-AD`, becomes `=H-G-AC-AD-AE`. Google Sheets auto-updates this when the column is inserted via `insertDimension` API call.

### Migration step

A one-time `insertDimension` API call at column index 25 (zero-based) shifts all existing sheet data before the formatter is re-applied. This is identical to how X and Y were inserted previously.

**`tools/migrate_columns.py`:** Add a `migrate_add_total_cost_col()` function that:
1. Calls `insertDimension` at index 25 on the Product Tracker tab
2. Writes the `=IFERROR(G+AD,G)` formula to the Z column header row and down the data range
3. Is safe to re-run (checks if Z header already says "TOTAL COST" before inserting)

---

## Files Changed

| File | Change |
|------|--------|
| `config/col_map.yaml` | Add `total_cost: "Z"`, shift all hidden cols +1 |
| `tools/sheet_formatter.py` | New header, VISIBLE_COLS/HIDDEN_START bump, Z formula, Y conditional formatting split into teal/amber rules, legend update |
| `agents/researcher.py` | 3-line `free_ship_val` logic update |
| `tools/migrate_columns.py` | Add `migrate_add_total_cost_col()` one-time migration function |

No other agent tools require changes — all write to hidden cols via `COL["key"]` lookups in `col_map.yaml`.

---

## Execution Order

1. Run `migrate_add_total_cost_col()` — inserts the column in the live sheet
2. Update `config/col_map.yaml` — new letters active for all future agent runs
3. Run `python agents/setup_sheet.py` — applies new formatting, headers, widths, conditional rules
4. Verify: Row 16 (and any other paid-shipping item) shows amber `$X.XX ship` in Y and total in Z
