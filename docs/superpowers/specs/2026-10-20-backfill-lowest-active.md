# Backfill lowest_active (col BF) for existing listings (Oct 20 2026)

## Goal

One-time backfill of `lowest_active` (col BF) for rows already in the pipeline, so the
"⚡ Competitive" button (commit c5c2689) works on Jay's *existing* SCORED/READY/APPROVED/ACTIVE
rows instead of saying "no live competitors scraped".

## Change

Add a new scheduler mode `backfill_lowest` that re-scrapes eBay active comps for those rows and
writes ONLY col BF. Nothing else is touched — never status / cost / price / suggested price.

### 1. `agents/scheduler.py` — new mode

Add a `run_backfill_lowest(config, COL, service, sheet_name, start_row, end_row)` function and
register `backfill_lowest` in `main()`'s `--mode` dispatch (same pattern as the other modes).

Logic:
1. Read the sheet out to **BF** (`'{sheet_name}'!A{start}:BF{end}`) — col BF must be included.
2. Filter to rows where `status` ∈ `{SCORED, READY, APPROVED, ACTIVE}` AND `title` (C) non-blank.
   (Make the status set a module-level constant so it's trivially adjustable.) Leave `lowest_active`
   (BF) blank-or-filled — a re-run refreshes the value, so the mode is idempotent.
3. Open **one** Chrome via `make_browser()` (from `tools.costco_scraper`) and reuse the page for
   every row — mirror how `researcher.py` manages the browser. Requires Chrome running (same as
   `research`/`active`). Close it in a `finally`.
4. For each row, call
   `get_ebay_comps(title, category, page=page)` from `tools.ebay_research` with `brand=None,
   model=None, ebay_category_id=None` (title-based query, unscoped category — correct for this).
   If `result["lowest_active"]` is not None, write `col BF = lowest_active` via
   `write_row_partial`. If it is None, leave the existing value untouched (don't wipe a good value
   with a blank). Wrap each row in try/except and continue on error (log it).
   `time.sleep(random.uniform(1, 2))` between rows (eBay rate limit).
5. Send a Telegram summary via `tools.alert_sender` (or the existing run-log pattern):
   `⚡ Backfilled cheapest-competitor price: N of M rows (K no-comps, E errors)`.
6. Support `--limit N` (cap rows processed) and `--dry-run` (print the plan, write nothing).

### 2. `agents/telegram_bot.py` — expose it in Operations

- Add `"backfill_lowest"` to `_JOB_MODES`.
- Add `"backfill_lowest": "⚡ Backfill competitive prices"` to `_JOB_LABELS`.

The existing `cb_job_start` already launches `python agents/scheduler.py --mode <mode>`, so no
other wiring is needed.

### 3. Tests

- `tests/test_ebay_research.py` already covers `lowest_active` capture.
- Add a light test (mirror the existing `_JOB_MODES`/`_JOB_LABELS` test if one exists) asserting
  `backfill_lowest` is in `_JOB_MODES` and has a label in `_JOB_LABELS`.
- If there's a mode-dispatch test in `tests/test_scheduler*`, add the new mode there too.

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: backfill_lowest mode to populate col BF for existing rows`.
- Push to `master`, reply `DONE — pushed`.
