# Shipping Cost Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update Col Y to show actual shipping cost when not free, and add a new visible Col Z (TOTAL COST) showing the all-in Costco + shipping price as a formula.

**Architecture:** Four-file change: col_map.yaml gets new letters; researcher.py gets updated badge logic; migrate_columns.py gets a one-time column-insert function; sheet_formatter.py gets updated headers, constants, conditional rules, and legend. Migration runs first to insert the column in the live sheet, then setup_sheet re-applies formatting.

**Tech Stack:** Python 3, Google Sheets API v4, pytest

---

## File Map

| File | Change |
|------|--------|
| `config/col_map.yaml` | Add `total_cost: "Z"`, shift all hidden col letters +1 |
| `agents/researcher.py` | Update `_ship_badge()` helper + `free_ship_val` logic |
| `tools/migrate_columns.py` | Add `migrate_add_total_cost_col()` |
| `tools/sheet_formatter.py` | Headers, widths, constants, Y formatting split, legend |
| `tests/test_ship_badge.py` | New — unit tests for badge logic |

---

## Task 1: Update col_map.yaml

**Files:**
- Modify: `config/col_map.yaml`

- [ ] **Step 1: Replace the visible section and all hidden column letters**

Replace the entire visible + hidden sections of `config/col_map.yaml` with:

```yaml
# Google Sheet column letter mappings.
# Matches the "Product Tracker" tab exactly.
# Update here if columns are ever added or reordered.
#
# Layout philosophy: action-critical columns first (A–D always frozen),
# monitoring data next (E–Z visible), everything else hidden (AA–AV).
#
# Formula columns — never overwrite these with agent writes:
#   I  (net_profit):      =H-G-AC-AD-AE  (ebay_price - eBay_fees - ship - fulfillment - cost)
#   J  (net_margin):      =IF(H>0,I/H,0)
#   N  (comp_saturation): =IFERROR(M/MAX(K,1),"")
#   AC (ebay_fees):       =H*AB       (ebay_price * fee_rate)
#   AF (tax_est):         =G*0.0825
#   AG (site_profit):     =H*0.90-G
#   AH (ad_budget):       =I*0.15
#   Z  (total_cost):      =IFERROR(G+AD,G)  (costco cost + shipping)
#
# NOTE: Col Z (total_cost) inserted 2026-05-21. All former hidden cols Z–AV
# shifted 1 right → AA–AV. Google Sheets auto-updates formula references on insert.

columns:
  # ── Visible (A–Z) ─────────────────────────────────────────────────
  status:           "A"   # PENDING/APPROVED/READY/ACTIVE/WATCH/PAUSED_*/
  demand_score:     "B"   # Tier score 0–10 (Tier 1≥7, Tier 2≥4, Tier 3<4)
  title:            "C"   # Product name
  category:         "D"   # Precious Metals / Jewelry / Watches / Outdoor Furniture
  platform:         "E"   # eBay / Site / Both (set when listed)
  stock_status:     "F"   # In Stock / Available (2/day limit) / OUT OF STOCK
  costco_cost:      "G"   # What you pay at Costco
  ebay_price:       "H"   # Your eBay listing price (editable — agent sets once)
  net_profit:       "I"   # formula — do not overwrite
  net_margin:       "J"   # formula — do not overwrite
  sold_90d:         "K"   # eBay sold listings (90 days)
  avg_price:        "L"   # Avg eBay sold price
  comp_count:       "M"   # Active competing eBay listings
  comp_saturation:  "N"   # formula — active÷sold ratio (Low/Med/High color-coded)
  last_checked:     "O"   # Agent last run timestamp
  price_change:     "P"   # Flag: price moved since last check
  ebay_listing_url: "Q"   # Paste live eBay listing URL here
  costco_url:       "R"   # Source product link
  re_eval_date:     "S"   # PAUSED items: date to re-research
  tier_summary:     "T"   # Short summary: [T2 | Score 8.2 | Sugg: $899 | margin 18%]
  units_sold:       "U"   # Units sold (manually entered or future eBay API)
  suggested_price:  "V"   # Agent's recommended eBay price — written once, never overwritten
  purchase_limit:   "W"   # Units/day limit (precious metals) or blank for fashion items
  sale_info:        "X"   # Sale badge: 🔥 -$150 ends 5/31 (blank if not on sale)
  free_shipping:    "Y"   # Ship badge: ✓ FREE or $12.99 ship (blank if unknown)
  total_cost:       "Z"   # formula =IFERROR(G+AD,G) — Costco cost + shipping; do not overwrite

  # ── Hidden (AA–AV) ────────────────────────────────────────────────
  sku:              "AA"
  fee_rate:         "AB"
  ebay_fees:        "AC"  # formula =H*AB — do not overwrite
  ship_cost:        "AD"
  fulfillment:      "AE"
  tax_est:          "AF"  # formula =G*0.0825 — do not overwrite
  site_profit:      "AG"  # formula =H*0.90-G — do not overwrite
  ad_budget:        "AH"  # formula =I*0.15 — do not overwrite
  seo_title:        "AI"
  bullets:          "AJ"
  description:      "AK"
  redirect_msg:     "AL"
  meta_desc:        "AM"
  keywords:         "AN"
  alt_text:         "AO"
  google_hl:        "AP"
  google_desc:      "AQ"
  meta_text:        "AR"
  meta_hl:          "AS"
  image_urls:       "AT"
  perf_score:       "AU"  # Composite performance score (0–10) — written by rotation engine
  full_notes:       "AV"  # Full research narrative, eBay comps, community signals (hidden)
```

- [ ] **Step 2: Verify the key count is correct**

```bash
python -c "
import yaml
with open('config/col_map.yaml') as f:
    cols = yaml.safe_load(f)['columns']
print(f'Total keys: {len(cols)}')
print(f'total_cost → {cols[\"total_cost\"]}')
print(f'ship_cost  → {cols[\"ship_cost\"]}')
print(f'full_notes → {cols[\"full_notes\"]}')
"
```

Expected output:
```
Total keys: 47
total_cost → Z
ship_cost  → AD
full_notes → AV
```

- [ ] **Step 3: Commit**

```bash
git add config/col_map.yaml
git commit -m "feat: add total_cost col Z to col_map, shift hidden cols AA-AV"
```

---

## Task 2: Badge Logic in researcher.py (TDD)

**Files:**
- Modify: `agents/researcher.py:896-902`
- Create: `tests/test_ship_badge.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_ship_badge.py`:

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.researcher import _ship_badge


def test_free_shipping_returns_check_free():
    assert _ship_badge(free_shipping=True, cart_est={}) == "✓ FREE"


def test_free_shipping_wins_even_when_cart_has_cost():
    # free_shipping flag from product page overrides cart_est
    assert _ship_badge(free_shipping=True, cart_est={"shipping": 12.99}) == "✓ FREE"


def test_paid_shipping_shows_dollar_amount():
    assert _ship_badge(free_shipping=False, cart_est={"shipping": 12.99}) == "$12.99 ship"


def test_paid_shipping_zero_shows_zero():
    assert _ship_badge(free_shipping=False, cart_est={"shipping": 0.0}) == "$0.00 ship"


def test_no_cart_estimate_returns_blank():
    assert _ship_badge(free_shipping=False, cart_est={}) == ""


def test_cart_estimate_none_value_returns_blank():
    assert _ship_badge(free_shipping=False, cart_est={"shipping": None}) == ""
```

- [ ] **Step 2: Run tests — expect failure (function not defined)**

```bash
pytest tests/test_ship_badge.py -v
```

Expected: `ImportError: cannot import name '_ship_badge' from 'agents.researcher'`

- [ ] **Step 3: Add `_ship_badge` helper and update call site in researcher.py**

In `agents/researcher.py`, add the helper **before** the `_suggest_ebay_price` function (search for `def _suggest_ebay_price`):

```python
def _ship_badge(free_shipping: bool, cart_est: dict) -> str:
    if free_shipping:
        return "✓ FREE"
    ship = cart_est.get("shipping")
    if ship is not None:
        return f"${ship:.2f} ship"
    return ""
```

Then find and replace the existing badge line (around line 902):

Old:
```python
            free_ship_val = "✓ FREE" if free_shipping else ""
```

New:
```python
            free_ship_val = _ship_badge(free_shipping, cart_est)
```

- [ ] **Step 4: Run tests — expect all pass**

```bash
pytest tests/test_ship_badge.py -v
```

Expected:
```
PASSED tests/test_ship_badge.py::test_free_shipping_returns_check_free
PASSED tests/test_ship_badge.py::test_free_shipping_wins_even_when_cart_has_cost
PASSED tests/test_ship_badge.py::test_paid_shipping_shows_dollar_amount
PASSED tests/test_ship_badge.py::test_paid_shipping_zero_shows_zero
PASSED tests/test_ship_badge.py::test_no_cart_estimate_returns_blank
PASSED tests/test_ship_badge.py::test_cart_estimate_none_value_returns_blank
6 passed
```

- [ ] **Step 5: Commit**

```bash
git add agents/researcher.py tests/test_ship_badge.py
git commit -m "feat: show paid shipping cost in Col Y badge"
```

---

## Task 3: Add Migration Function

**Files:**
- Modify: `tools/migrate_columns.py`

The existing `migrate_columns.py` handles old-layout → new-layout data moves. We add a new, independent function for this column insertion. It is idempotent — safe to re-run.

- [ ] **Step 1: Add `migrate_add_total_cost_col()` to migrate_columns.py**

Append to the end of `tools/migrate_columns.py` (before `if __name__ == "__main__"`):

```python
def migrate_add_total_cost_col(dry_run=False):
    """
    One-time migration: insert a new visible column Z (TOTAL COST) in the
    Product Tracker tab, shifting all former hidden cols (Z–AU) right by one.

    Safe to re-run — checks the Z header before inserting.
    Must run BEFORE col_map.yaml is updated (or the idempotency check still works
    because it reads the sheet directly, not col_map).
    """
    service    = get_sheets_service()
    sheet_id   = os.getenv("GOOGLE_SHEET_ID")
    sheet_name = "Product Tracker"

    # ── Idempotency check: read current col Z header (index 25) ──────────────
    check = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!Z3",   # row 3 is the column header row
    ).execute()
    current_z = (check.get("values") or [[""]])[0][0] if check.get("values") else ""
    if current_z == "TOTAL COST":
        print("Col Z already says 'TOTAL COST' — migration already applied. Skipping.")
        return

    print(f"Col Z currently shows: {current_z!r}. Proceeding with insert.")

    if dry_run:
        print("DRY RUN — would insert dimension at col index 25 (Z) and write IFERROR formulas.")
        return

    # ── Get tab sheetId ───────────────────────────────────────────────────────
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id, includeGridData=False
    ).execute()
    tab_id = None
    for s in meta["sheets"]:
        if s["properties"]["title"] == sheet_name:
            tab_id = s["properties"]["sheetId"]
            break
    if tab_id is None:
        raise ValueError(f"Tab '{sheet_name}' not found in spreadsheet.")

    # ── Insert one column at index 25 (zero-based = col Z) ───────────────────
    # Google Sheets auto-shifts all existing data and updates formula references.
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "insertDimension": {
                "range": {
                    "sheetId": tab_id,
                    "dimension": "COLUMNS",
                    "startIndex": 25,
                    "endIndex": 26,
                },
                "inheritFromBefore": False,
            }
        }]},
    ).execute()
    print("Column inserted at index 25 (col Z). Existing data shifted to AA+.")

    # ── Write TOTAL COST formulas to Z4:Z1000 ────────────────────────────────
    # ship_cost is now in AD (was AC before insert). Formula: =IFERROR(G+AD, G)
    formula_rows = [["=IFERROR(G{r}+AD{r},G{r})".format(r=row)] for row in range(4, 1001)]
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!Z4",
        valueInputOption="USER_ENTERED",
        body={"values": formula_rows},
    ).execute()
    print("TOTAL COST formulas written to Z4:Z1000.")

    print("Migration complete. Run 'python agents/setup_sheet.py' to apply formatting.")
```

Replace the existing `if __name__ == "__main__"` block at the bottom with:

```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="legacy",
                        choices=["legacy", "add-total-cost-col"],
                        help="Migration to run")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.command == "add-total-cost-col":
        migrate_add_total_cost_col(dry_run=args.dry_run)
    else:
        dry = args.dry_run
        if dry:
            print("=== DRY RUN ===")
        migrate(dry_run=dry)
```

- [ ] **Step 2: Dry-run to verify logic (no live changes)**

```bash
python tools/migrate_columns.py add-total-cost-col --dry-run
```

Expected (if not yet applied):
```
Col Z currently shows: 'SKU' (or similar hidden col value).
DRY RUN — would insert dimension at col index 25 (Z) and write IFERROR formulas.
```

Or if already applied:
```
Col Z already says 'TOTAL COST' — migration already applied. Skipping.
```

- [ ] **Step 3: Commit**

```bash
git add tools/migrate_columns.py
git commit -m "feat: add migrate_add_total_cost_col() for col Z insertion"
```

---

## Task 4: Update sheet_formatter.py

**Files:**
- Modify: `tools/sheet_formatter.py`

Four sub-changes: (a) constants, (b) Y conditional formatting split, (c) legend, (d) data-read range.

- [ ] **Step 1: Update HEADER_LABELS, COLUMN_WIDTHS, and constants**

In `tools/sheet_formatter.py`, find the `HEADER_LABELS` list (around line 54). Replace the last two entries and add Z:

Old (lines 78–81):
```python
    "SALE",            # X  23 — 🔥 -$150 ends 5/31 (blank if not on sale)
    "FREE SHIP",       # Y  24 — ✓ FREE (blank otherwise)
    # Z–AU hidden
]
```

New:
```python
    "SALE",            # X  23 — 🔥 -$150 ends 5/31 (blank if not on sale)
    "SHIP COST",       # Y  24 — ✓ FREE or $12.99 ship (blank if unknown)
    "TOTAL COST",      # Z  25 — formula =IFERROR(G+AD,G) — Costco cost + shipping
    # AA–AV hidden
]
```

In `COLUMN_WIDTHS`, add the new entry for index 25 (after the Y entry):

Old:
```python
    24: 80,    # Y: free shipping badge
}
```

New:
```python
    24: 90,    # Y: ship cost badge (wider — "$12.99 ship" needs more room than "✓ FREE")
    25: 110,   # Z: total cost
}
```

Update the constants block (around lines 111–117):

Old:
```python
VISIBLE_COLS  = 25    # A–Y
TOTAL_COLS    = 46    # A–AU
HIDDEN_START  = 25    # Z onwards (index 25 = col Z)
FROZEN_COLS   = 4     # A–D always visible

SALE_COL_IDX  = 23    # X — orange badge when non-empty
SHIP_COL_IDX  = 24    # Y — green badge when non-empty
```

New:
```python
VISIBLE_COLS  = 26    # A–Z
TOTAL_COLS    = 47    # A–AV
HIDDEN_START  = 26    # AA onwards (index 26 = col AA)
FROZEN_COLS   = 4     # A–D always visible

SALE_COL_IDX  = 23    # X — orange badge when non-empty
SHIP_COL_IDX  = 24    # Y — teal badge (FREE) or amber badge (paid ship)
```

Update the docstring at the top of the file (line 7):

Old:
```python
Columns Z–AU are hidden (SKU, fees, SEO copy, formulas, ad copy, image URLs, full notes).
```

New:
```python
Columns AA–AV are hidden (SKU, fees, SEO copy, formulas, ad copy, image URLs, full notes).
Col Z (TOTAL COST) is visible — formula =IFERROR(G+AD,G).
```

- [ ] **Step 2: Split Y conditional formatting into teal (FREE) + amber (paid ship)**

Find the `# 17d. Conditional formatting` block (around line 590). Replace the entire block:

Old:
```python
    # 17d. Conditional formatting — FREE SHIP col (Y = index 24): teal bg when non-empty
    ship_ref = f"$Y{data_start_row}"
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_cell_range(tab_id, data_row_idx, 1000, SHIP_COL_IDX, SHIP_COL_IDX + 1)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                   "values": [{"userEnteredValue": f'={ship_ref}<>""'}]},
                    "format": {"backgroundColor": _rgb("00897B"),
                               "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")}},
                },
            },
            "index": 0,
        }
    })
```

New:
```python
    # 17d. Conditional formatting — SHIP COST col (Y = index 24):
    #   teal when "✓ FREE", amber when "ship" (paid shipping cost)
    ship_ref = f"$Y{data_start_row}"
    # Rule: teal bg when cell contains FREE
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_cell_range(tab_id, data_row_idx, 1000, SHIP_COL_IDX, SHIP_COL_IDX + 1)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                   "values": [{"userEnteredValue": f'=ISNUMBER(SEARCH("FREE",{ship_ref}))'}]},
                    "format": {"backgroundColor": _rgb("00897B"),
                               "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")}},
                },
            },
            "index": 0,
        }
    })
    # Rule: amber bg when cell contains "ship" (paid shipping amount)
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_cell_range(tab_id, data_row_idx, 1000, SHIP_COL_IDX, SHIP_COL_IDX + 1)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                   "values": [{"userEnteredValue": f'=ISNUMBER(SEARCH("ship",{ship_ref}))'}]},
                    "format": {"backgroundColor": _rgb("F57C00"),
                               "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")}},
                },
            },
            "index": 0,
        }
    })
```

- [ ] **Step 3: Update legend COLUMN QUICK REFERENCE**

Find the Col Y and Col X legend rows (around line 736–738):

Old:
```python
    _row("Col X", "SALE — 🔥 -$150 ends 5/31 badge when product is on sale at Costco. Blank otherwise.", "", "")
    _row("Col Y", "FREE SHIP — ✓ FREE badge when Costco ships this product for free. Blank otherwise.", "", "")
    _row("Col V", "SUGG. PRICE — agent's one-time recommended price. Do not overwrite.", "", "")
    _row("Col W", "PURCH. LIMIT — Costco daily buy limit. Precious metals: 2/day.", "", "")
```

New:
```python
    _row("Col X", "SALE — 🔥 -$150 ends 5/31 badge when product is on sale at Costco. Blank otherwise.", "", "")
    _row("Col Y", "SHIP COST — ✓ FREE (teal) when free shipping; $12.99 ship (amber) when paid. Blank if unknown.", "", "")
    _row("Col Z", "TOTAL COST — formula: Costco cost + shipping. All-in delivered price. Never overwrite.", "", "")
    _row("Col V", "SUGG. PRICE — agent's one-time recommended price. Do not overwrite.", "", "")
    _row("Col W", "PURCH. LIMIT — Costco daily buy limit. Precious metals: 2/day.", "", "")
```

- [ ] **Step 4: Update the data read range in `setup_dashboard`**

Find the `all_data` read near line 636:

Old:
```python
        range=f"'{sheet_name}'!A4:AU1000",
```

New:
```python
        range=f"'{sheet_name}'!A4:AV1000",
```

- [ ] **Step 5: Commit**

```bash
git add tools/sheet_formatter.py
git commit -m "feat: update sheet_formatter for col Z (TOTAL COST) and Y amber/teal badge split"
```

---

## Task 5: Run Migration and Verify

**Prerequisites:** Tasks 1–4 complete. `.env` loaded with `GOOGLE_SHEET_ID`.

- [ ] **Step 1: Run the migration (inserts col Z in live sheet)**

```bash
python tools/migrate_columns.py add-total-cost-col
```

Expected:
```
Col Z currently shows: 'SKU' (or blank or similar).
Column inserted at index 25 (col Z). Existing data shifted to AA+.
TOTAL COST formulas written to Z4:Z1000.
Migration complete. Run 'python agents/setup_sheet.py' to apply formatting.
```

If you see `Col Z already says 'TOTAL COST' — migration already applied. Skipping.`, that's fine — move to the next step.

- [ ] **Step 2: Re-apply dashboard formatting**

```bash
python agents/setup_sheet.py
```

Expected: No errors. Sheet formatter runs without exception.

- [ ] **Step 3: Visual verification in the sheet**

Open the Google Sheet and confirm:

1. **Col Y header** says `SHIP COST`
2. **Col Z header** says `TOTAL COST`
3. **Row 16** (the item you noticed): Col Y shows `$X.XX ship` in amber, Col Z shows the sum
4. **A free-shipping item**: Col Y shows `✓ FREE` in teal, Col Z equals Col G exactly
5. **Col AA** (first hidden col) is hidden and contains SKU data (not shifted data)
6. Net profit (Col I) still calculates correctly — spot-check one row: `H - G - AC - AD - AE` matches Col I value

- [ ] **Step 4: Run the unit tests one final time**

```bash
pytest tests/test_ship_badge.py -v
```

Expected: 6 passed

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: verify shipping cost visibility feature complete"
```

---

## Self-Review Notes

- **Spec coverage:** All three spec sections (Col Y badge logic, Col Z formula column, hidden col shift + migration) have corresponding tasks. ✓
- **Type consistency:** `_ship_badge` defined in Task 2, imported in tests in Task 2. ✓
- **Migration idempotency:** Checks `Z3` cell value before inserting — safe to re-run. ✓
- **Formula references:** Col Z uses `=IFERROR(G+AD,G)` — AD is ship_cost after the shift. Matches col_map.yaml. ✓
- **Net profit formula:** `=H-G-AC-AD-AE` — Google Sheets auto-updates from `=H-G-AB-AC-AD` when column inserted. No manual edit needed. ✓
- **TOTAL_COLS:** Updated to 47 (A–AV). ✓
