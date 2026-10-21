# Fix descriptions — stop advertising Costco + false "Ships from Texas" (Oct 21 2026)

## Goal

Items drop-ship straight from Costco. So "Ships from Texas" is a false claim, and "Reference Costco
quality" advertises reselling another retailer's stock — a dropshipping-policy red flag. Remove both
from the copy and from every live description.

## Change

1. `tools/listing_copy.py` — remove "Reference Costco quality" from the Outdoor Furniture category
   rule (keep "brand name prominently" — the product's own brand is fine). Also grep the whole copy
   prompt + category rules for any other "Costco" reference and remove it.
2. **Footer** — change to exactly:
   `Sold by JA_Liquidations. eBay Money Back Guarantee applies.`
   (drop "Ships from Texas"). Update the footer constant wherever it lives (`listing_copy.py` and
   `tools/fix_listing_descriptions.py` `FOOTER`).
3. **Clean existing rows** — read col AK on live rows; for any description containing "Costco" or
   "Ships from Texas", strip it and set the new footer. Revise the live eBay description in place
   via the existing `revise_description` (no re-list).
4. `--dry-run` first — paste the before/after footer and the list of rows that will change.

## Acceptance

- `--dry-run` first, then run for real.
- Commit `fix: remove Costco references + false Ships-from-Texas from descriptions`, push to
  `master`, reply `DONE — pushed` with the count of rows changed.
