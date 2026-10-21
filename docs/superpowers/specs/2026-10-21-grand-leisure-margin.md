# Fix Grand Leisure rows 95/96 — no cost, no margin check (Oct 21 2026)

## Goal

Rows 95 and 96 (Grand Leisure City Heights + Las Palmas) are ACTIVE, live at $3,461.99 (qty 5), but
col G (cost) and col AB (fee rate) are blank after 8 active runs. With no cost there is no margin
check — `ebay_sync` marks them `margin_unchecked` and that never reaches Telegram, so two ~$3.5k
listings could be selling at a loss silently. Fill the missing data and surface `margin_unchecked`.

## Change

1. **Find the gap:** read `run_active_monitor` and work out why it has not written col G for 95/96
   after 8 runs (they have col R Costco URLs: `.product.100404264.html` / `.product.100404055.html`).
   Fix whatever is skipping the cost write for these two rows.
2. Fill col G (live Costco cost) and col AB (Outdoor Furniture fee rate) for rows 95/96 via a scrape
   (Chrome, local) — or the normal monitor path once the gap is fixed.
3. **Surface `margin_unchecked`:** in `ebay_sync`, add a Telegram alert for rows marked
   `margin_unchecked` (live listing, blank cost/fee) so a listing with no margin data is never silent.
4. `--dry-run` / read-only first where a scrape is involved.

## Acceptance

- After the fix, rows 95/96 have col G + AB and a real margin check (or an explicit alert that they
  are unpriced).
- `margin_unchecked` rows reach Telegram.
- Run the safe suite. Commit `fix: fill Grand Leisure cost/fee + alert margin_unchecked`, push to
  `master`, reply `DONE — pushed` with what was found and fixed.
