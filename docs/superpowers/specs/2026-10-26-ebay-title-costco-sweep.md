# Permanent "no Costco in titles" sweep on every eBay sync (Oct 26 2026)

## Goal

Make "no live eBay title contains `Costco`" a standing invariant, enforced on every `ebay_sync`
run, instead of a one-off manual sweep. The 10/21 cleanup scrubbed descriptions only; titles were
never swept, and a 10/26 manual sweep found **13** live/hidden titles still carrying `Costco`
(trailing, "– Costco", "Costco Quality", "Costco Brand", "Costco Formula", "Costco 2024", bare
mid-title). Jay's rule: never say Costco in listings — it is a dropshipping red flag and the same
risk class as the Henredon 21920397 takedown.

## Change

All in `tools/ebay_sync.py` (the sweep reads titles `fetch_active_listings()` already returns, so
no extra eBay read; the only eBay write is the rare revise).

### Part 1 — `_strip_costco_title(title) -> str` (pure, deterministic)

Remove `Costco` and its adjacent connector/qualifier words, then return a clean title capped at
`MAX_TITLE_LEN` (80). Three ordered passes (all `re.I`):

1. Qualified: `\bCostco\s+(?:Quality|Brand|Formula|Select|Premium|\d{4})\b` → `""`
2. Dangling connector: `\s*[-–—]\s*Costco\b` → `""`
3. Bare word: `\bCostco\b` → `""`

Then `re.sub(r"\s{2,}", " ", t).strip(" \t,;:-–—")` and return `t[:MAX_TITLE_LEN]`.

**Never** remove `Kirkland` (it is the brand, not the retailer) or any other word. This is REMOVAL,
not the description rewrite — do not turn "Costco Quality" into "Premium Quality"; drop it entirely.

Ground-truth cases the tests must pass:

| Before | After |
|---|---|
| `Grand Leisure Las Palmas 6-Piece Outdoor Patio Seating Set - Costco Quality` | `Grand Leisure Las Palmas 6-Piece Outdoor Patio Seating Set` |
| `Kirkland Signature Calcium 600 mg Costco` | `Kirkland Signature Calcium 600 mg` |
| `Organic Soy Beverage Costco Non-Dairy` | `Organic Soy Beverage Non-Dairy` |
| `SunVilla Sara – Costco` | `SunVilla Sara` |
| `Protein Bar Variety – Costco Brand` | `Protein Bar Variety` |
| `Transformer Table Costco 2024` | `Transformer Table` |
| `Multivitamin Gummies Costco Formula` | `Multivitamin Gummies` |
| `Kirkland Signature Organic Quinoa` (no Costco) | unchanged |

### Part 2 — `sweep_titles(listings, report, service, COL, sheet_name, *, dry_run, title_reader, enabled=True) -> list`

1. Build `row_by_id = {e["item_id"]: e["row_num"] for e in report["matched"]}`.
2. For each `listing` in `listings` whose title contains `costco` (case-insensitive word match):
   - `clean = _strip_costco_title(listing["title"])`.
   - Skip if `not clean`, `clean == listing["title"]`, `len(clean) > MAX_TITLE_LEN`, or
     `listing.get("listing_type") not in (None, "FixedPriceItem")` (mirror the `revise_title` guard).
   - `outcome = "dry_run"` if dry_run; else call `revise_title(item_id, clean)` and set
     `outcome = "revised"` / `"failed"` from its return (`ok` / `error_kind`).
   - If the item has a sheet row (`item_id in row_by_id`), write the clean title to col AI via
     `write_row_partial(service, sheet_name, row_by_id[item_id], [(COL["seo_title"], clean)])` only
     when `outcome == "revised"` and not dry_run — keep sheet == eBay.
   - Collect `{item_id, title, clean_title, row_num?, outcome, error?}`.
3. Return the results list.

Use the same `title_reader` guard pattern as `auto_reprice_breaches` (a row whose title changed
since the read is skipped, not written) so the sheet write can't target a stale row.

### Part 3 — `format_title_sweep_summary(results) -> str | None`

Return `None` when nothing was revised (dry-run and clean runs stay silent). Else one short line,
e.g. `🔤 Title sweep: 2 listing(s) revised (removed "Costco")` plus one item-id line each for any
`failed` revise. Mirror the tone/length of `format_auto_reprice_summary` / `format_mirror_summary`.

### Part 4 — hook in `run_ebay_sync`

After the `sync(...)` call (report is built, `matched` has row numbers) and before `alert_message`,
add a try/except block that calls `sweep_titles(...)` with
`enabled=bool(auto_cfg.get("sweep_titles", True))`, `title_reader=lambda: _read_titles(...)`, and
`dry_run=dry_run`; append `format_title_sweep_summary(...)` to the `extras` list. A failure must
never break the sync (same `logger.exception` pattern as the other steps).

## Pitfalls (do not skip)

- **Revise, never re-list.** `revise_title` already does ReviseFixedPriceItem in place; reuse it.
- **Only revise when the title actually changes.** A listing whose title is already clean must be a
  no-op — no eBay call, no sheet write.
- **Orphans count too.** `listings` includes items with no sheet row (`report["on_ebay_not_in_sheet"]`);
  sweep + revise them, just don't try a sheet write (no `row_num`).
- **Hidden listings are covered by design.** `fetch_active_listings()` returns only active items; a
  listing hidden at quantity 0 is invisible to buyers, and when it restocks it becomes active again
  and is swept on the next sync (4x/day). Do NOT add a GetItem loop for hidden items — that is out
  of scope.
- **`dry_run` does zero writes** — no revise, no sheet update, but still returns the plan so the
  summary can show what WOULD change.

## Acceptance

- 5+ tests: one per ground-truth case above in `tests/test_ebay_sync_title_sweep.py`, plus one
  sweep test with `revise_title` / `write_row_partial` stubbed proving orphan revise + matched sheet
  write + dry-run-no-write.
- Full pytest suite passes on Windows.
- `--dry-run` on the real sheet lists 0 revisions (all 60 live titles already cleaned 10/26) and
  sends nothing — proving the sweep is a silent no-op when clean.
- Commit `feat: ebay_sync sweeps "Costco" out of live listing titles every run`, push to `master`,
  reply `DONE — pushed` with test count.
