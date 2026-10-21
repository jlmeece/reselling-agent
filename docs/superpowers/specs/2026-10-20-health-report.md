# Health Report — full-sheet ground truth (Oct 20 2026)

## Goal

A read-only `--mode health` that produces ONE complete picture of the sheet so Jay can see,
without hunting, exactly what's in each status, what's stale, and what's in Graveyard. This is
the ground truth for the audit — no writes, no guesses.

## Change

Add `run_health(config, COL, service, sheet_name, start_row, end_row, check_costs=False)` to
`agents/scheduler.py` + register `health` in `main()`'s `--mode` choices/dispatch (mirror the
other modes). Reuse `read_sheet`, `read_graveyard` (tools/graveyard_writer), `scrape_costco`.

### Part 1 — sheet + Graveyard dump (no Chrome, always runs)

Read Product Tracker `A:BF` and Graveyard via `read_graveyard(service)`. Report, in this order:

1. **Count by status** — one line per status present (e.g. `ACTIVE 12 · READY 5 · SCORED 18 ·
   PAUSED_OOS 3 · AUDIT_REVIEW 7 · …`).
2. **AUDIT_REVIEW rows** — every one: `title · reason (col T) · last-checked (col O)`. These are
   items parked for Jay's decision and are the most likely to feel "missing".
3. **PAUSED_* rows** — every one: `title · status · last-checked`.
4. **Stale rows** — every non-Graveyard row whose `last_checked` (O) is blank OR older than
   `STALE_DAYS` (default 7): `title · status · last-checked`.
5. **Blank-critical-field rows** — rows with a blank cost (G), blank eBay price (H), or ACTIVE
   with a blank col Q (unlinked live listing).
6. **Graveyard** — count + the most recent 20 removals: `title · removed <date> · reason`.

Send as ONE Telegram message (chunked via `_chunk_lines`-style logic / the scheduler's existing
`_send_telegram`). `--dry-run` prints it instead of sending.

### Part 2 — cost staleness check (Chrome, only with `--check-costs`)

For every non-Graveyard row with a `costco_url` starting with `http`, `scrape_costco` and compare
the live price to col G. Collect every row where they differ by ≥ $0.50 into a
`⚠️ Cost mismatch (N)` list: `title · sheet $X → live $Y · last-checked <date>`. Report the count
even when it's zero. One Chrome page reused (mirror `run_backfill_lowest`); 1–2s between rows.
Does NOT write anything.

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: --mode health read-only full-sheet report (status counts, stale, Graveyard, cost mismatches)`,
  push to `master`, reply `DONE — pushed` with a paste of the report it produced on `--dry-run`
  (so Hermes can see the output shape).
