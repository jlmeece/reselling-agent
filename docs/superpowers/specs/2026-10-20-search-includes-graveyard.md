# Search across Product Tracker + Graveyard (Oct 20 2026)

## Goal

`/lookup <term>` and the 🔍 Search button only search the main Product Tracker tab. A product the
auditor moved to Graveyard looks "missing" — Jay has now hunted for 4 of them (Grand Leisure City
Heights / Las Palmas, Kirkland Calcium, POLYWOOD Long Beach Modern). Make search report Graveyard
hits too, so a removed product shows "removed <date> · <reason>" instead of "No products found."

## Change (agents/telegram_bot.py)

1. Import the existing reader: `from tools.graveyard_writer import read_graveyard`.
2. In the search flow (both `_handle_search_term` and the `/lookup` path — they share
   `search_products` + `format_lookup_reply`), after the Product Tracker matches are resolved,
   also fetch Graveyard rows once via `read_graveyard(service)` and match the term against
   `title` (case-insensitive substring, same rule as `search_products`; also match `sku` and
   `costco_url` product-id if the term looks like a number/SKU).
3. Merge into the reply:
   - Product Tracker matches render exactly as today (single → full card; 2–5 → summary lines).
   - Then, if there are Graveyard matches, append a `🪦 Graveyard (N)` section. One line each:
     `🪦 {title} — removed {date_removed} · {reason}` (truncate title to ~48 chars, reason to ~40;
     if `costco_url` is present append the product id in parens, if `sku` present append `#sku`).
   - "No products found" now fires ONLY when there are zero matches in BOTH tabs. When Product
     Tracker is empty but Graveyard has matches, show the Graveyard section (not "no products").
4. Cap the Graveyard section at 5 lines (`…and N more in Graveyard`) so a broad term can't blow
   the 4096-char message. Reuse `_chunk_lines` if needed.

## Tests (tests/test_telegram_bot.py)

- A term that matches only Graveyard returns the `🪦 Graveyard` section, not "No products found".
- A term that matches both tabs renders Product Tracker first, then the Graveyard section.
- A term matching neither still returns "No products found".
- Graveyard match line carries the removed date + reason (and sku when present).

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: search also reports Graveyard hits (removed date + reason)`, push to `master`,
  reply `DONE — pushed`.
