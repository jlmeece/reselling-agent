# Full code audit — hunt the five bug classes (Oct 21 2026)

## Goal

Read-only audit of the reselling tool for five specific bug classes that have already caused real
losses this month. Produce a **ranked findings list**, no auto-fixes, no wall of text. We triage
together before anything is changed.

## The five classes to hunt

1. **Silent writes** — any sheet/eBay change with no Telegram notification to Jay.
2. **Status-gating gaps** — any live eBay listing (col Q) that goes unmonitored (stock / price /
   margin / sale-end) because of a non-ACTIVE sheet status.
3. **Loss-protection holes** — any path where a live listing can sell below break-even (net < 0)
   with no auto-reprice or alert.
4. **eBay policy landmines** — description/photo/template content that could get a listing hidden
   (off-eBay redirect, external URLs, contact info, marketplace badges, false claims).
5. **Data drift** — col_map / formula / HEADER_LABELS mismatches, or any write that can orphan a
   column or ship silent drift.

## Method (read-only + dry-run — NO live changes)

- Static: read the relevant paths — `agents/scheduler.py`, `agents/auditor.py`, `tools/ebay_sync.py`,
  `tools/sheet_writer.py`, `tools/ebay_export.py`, `tools/listing_copy.py`,
  `tools/photo_compositor.py`, `tools/status_logic.py`, `config/col_map.yaml`.
- Smoke-test each `--mode` (audit, daily, active, ebay_sync, export, sale-digest, health,
  research, rotation) with `--dry-run` where it exists, to catch crashes/errors without writing.
- Do NOT run anything for real. Do NOT write to eBay or the sheet.

## Output contract (keep it SHORT — ≤ 30 lines)

Reply with exactly this shape:

- `CRITICAL` — one line each: what, where (file), why it matters, suggested fix.
- `MINOR` — one line each.
- `CONFIRMED CLEAN` — the areas checked and found safe.
- Final line: `N critical · M minor · K areas clean`.

Do NOT fix anything. Do NOT dump code. Do NOT commit code changes.

## Acceptance

- Read-only; no code commit. Reply with the findings report text for Jay + Hermes to triage.
