# Revive Grand Leisure City Heights + Las Palmas (Oct 20 2026)

## Goal

Bring two patio sets back into the pipeline after the auditor moved them to the Graveyard
(10/17, "Zero velocity + net < $5") while their eBay listings were still live. Their listings
were hidden (quantity 0) during the 10/19 orphan triage. Restore them: re-activate the hidden
eBay listings and re-link them to fresh ACTIVE sheet rows.

## Products (exact data — from `data/knowledge/products/`)

| field | City Heights | Las Palmas |
|-------|--------------|------------|
| title | Grand Leisure City Heights 6-piece Outdoor Patio Seating Set | Grand Leisure Las Palmas 6-piece Outdoor Patio Seating Set |
| category | Outdoor Furniture | Outdoor Furniture |
| costco_url | https://www.costco.com/.product.100404264.html | https://www.costco.com/.product.100404055.html |
| sku (col AA) | 100404264 | 100404055 |
| restore quantity | 5 | 5 |

## Change

Add a one-off script `tools/revive_grand_leisure.py` (idempotent; re-runnable safely). It:

1. Loads `.env` (sheet + eBay creds) exactly like the other tools.
2. `from tools.ebay_sync import fetch_active_listings, set_quantity`.
   `fetch_active_listings()` → find each product by **title** (case-insensitive `in` match on the
   product name, e.g. `"city heights"` / `"las palmas"`). The two hidden listings are still in
   ActiveList (GTC, OutOfStockControl, quantity 0).
3. **Match guard:** for each product, take the unique listing whose title contains the product
   name. If 0 matches → report "not found, skipped". If >1 matches → report ALL matches and SKIP
   (never raise quantity on a guess).
4. For each matched listing: `set_quantity(item_id, 5)` (the existing ReviseInventoryStatus path
   used by the OOS restore). Report `item_id → qty 0 → 5`.
5. Sheet write (via `tools.sheet_writer`): if a Product Tracker row already has this exact title,
   UPDATE it; otherwise APPEND a new row. Either way write:
   - status (A) = `ACTIVE`
   - title (C) = the exact title above
   - category (D) = `Outdoor Furniture`
   - ebay_price (H) = the listing's `price` from the fetch (its live Buy It Now price)
   - ebay_listing_url (Q) = the bare item id (extract_item_id accepts it)
   - costco_url (R) = the `.product.<id>.html` URL above
   - sku (AA) = the product id
   Do NOT touch any other column.
6. Print + Telegram-free summary of what it did (item ids, quantities, sheet rows written).
7. `--dry-run` flag: print the plan (matches + what would be written) and do nothing.

## Acceptance

- Run the curated safe suite (unaffected). Commit message:
  `feat: revive Grand Leisure City Heights + Las Palmas (re-activate listings + relink)`,
  push to `master`, reply `DONE — pushed` WITH the two item ids + quantities it set.
