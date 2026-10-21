# Minor sweep — fix the non-critical audit findings in one batch (Oct 21 2026)

## Goal

One batch closing the MINOR findings from the full code audit. None are urgent; all are
correctness / notification / hygiene fixes. Each is small and testable.

## Change

1. **Rotation crash** — `rotation_engine.py` KeyError `'notes'` (crashed weekly since 10/04). Use
   `tier_summary` instead of the missing key.
2. **Blank col BF header** — run `refresh_header_row` so the BF header in row 3 is populated.
3. **Dead config** — remove `coming-soon.com` and `SAVE10` from `config/categories.yaml` (the copy
   prompt no longer uses them).
4. **AE header mismatch** — the AE header says "FULFILLMENT" but the column is `ad_cost`; fix the label.
5. **Legend stale formula** — update the Legend to the current net formula (`H-G-AC-AD-AE-AF`).
6. **Dry-run side-effects** — a `--dry-run` must NOT write a Run Log row, and `ebay_sync --dry-run`
   must NOT send Telegram. Make dry-run truly read-only.
7. **Auditor safety** — re-check the title (not just the row number) before deleting a row, and never
   fall back to sheet id 0 when the sheet lookup fails.
8. **Silent skip** — the monitor skips a live row that has no Costco URL without alerting; add a
   Telegram notice so it's never silent.
9. **Silent status changes** — daily-sweep status moves (APPROVED→READY, PAUSED_*→WATCH, →PENDING)
   go email-only; also send them to Telegram (matches the no-silent-changes rule).
10. **Silent col H overwrite** — `ebay_sync`'s col H price mirror overwrites a hand edit without a
    message; send a Telegram note when it does.

## Acceptance

- Each fix has a test. Run the full safe suite.
- Commit `fix: minor sweep (audit findings)`, push to `master`, reply `DONE — pushed` with one line
  per item (fixed / skipped + why).
