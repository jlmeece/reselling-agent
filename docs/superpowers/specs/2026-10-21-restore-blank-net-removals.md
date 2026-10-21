# Restore blank-net (net $0.00) Graveyard removals (Oct 21 2026)

## Goal

On 10/18–10/19 the auditor ran a stale copy with the old blank-net bug: a blank net-profit
cell was read as $0.00, so unpriced products were auto-removed with the exact reason
`Below floor ($0.50 min) — net $0.00` (14 confirmed in the health report; there may be more
from earlier dates). The bug is fixed in current code (blank net → `None`, skip economics;
`_parse_net` + regression test `test_unpriced_pending_row_is_not_removed_by_economics_rules`).
This script re-adds the wrongly-removed items as PENDING so they get researched/priced fresh.

## Change (tools/restore_blank_net_removals.py — one-off, idempotent)

1. Load `.env` and `get_sheets_service()` exactly like the other tools (no Chrome needed).
2. `read_graveyard(service)` → keep only rows whose `reason` is **exactly**
   `Below floor ($0.50 min) — net $0.00` (the old bug's signature; no current code emits it).
3. Read the Product Tracker (title, costco_url, sku) to dedupe.
4. For each Graveyard match, **skip** if a Product Tracker row already has the same Costco
   product id (from `costco_url` via `sched.costco_product_id`) **or** the same title
   (case-insensitive). (The Serpentine bracelet is already back as PENDING on r86 — must not
   duplicate.)
5. For each remaining match, APPEND a new Product Tracker row:
   - status (A) = `PENDING`
   - title (C), category (D), costco_url (R), sku (AA) — copied from the Graveyard row
   - leave cost (G), ebay_price (H), net (I) **blank** — re-researched fresh, no stale cost.
   - Do NOT touch any other column.
6. `--dry-run` prints the exact plan: the count matched, which titles would be re-added, and
   which are skipped as already-present. Real run writes via `tools/sheet_writer`.
7. Idempotent — re-running skips rows it already restored (same dedupe in step 4).

## Acceptance

- Run `--dry-run` first and paste the plan back for Jay to review. Then run for real.
- Commit `feat: restore blank-net (net $0.00) Graveyard removals as PENDING`, push to `master`,
  reply `DONE — pushed` with the count restored + which were skipped.
