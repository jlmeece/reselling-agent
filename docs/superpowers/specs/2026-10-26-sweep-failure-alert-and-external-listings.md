# Sweep failure alert + exclude external eBay listings (Oct 26 2026)

## Goal

Two small follow-ups to the title sweep (spec 2026-10-26-ebay-title-costco-sweep):

1. A run where **every title revise fails** must alert. Today `format_title_sweep_summary`
   returns `None` unless at least one title was revised, so an all-failed run is silent and the
   failure only shows in the log — the exact safety mechanism just built can fail invisibly.
2. Jay sells some products on the **same eBay account** OUTSIDE the Costco→eBay reselling tool
   (e.g. "RoboTabs" cough suppressant, eBay 860069578501). They are intentional, not tracking
   errors, and must not appear as "On eBay but not in the sheet" orphans on every sync.

## Change

All in `tools/ebay_sync.py` + `config/categories.yaml`.

### Part 1 — failed revises always alert (`format_title_sweep_summary`)

Collect `revised` and `failed` separately. Return `None` only when BOTH are empty. Header:
- some revised → `🔤 <b>Title sweep:</b> N listing(s) revised (removed "Costco")`
- none revised but ≥1 failed → `🔤 <b>Title sweep:</b> ⚠️ N revise(s) FAILED` (still list each
  `• ❌ item …` line as today).

Dry-run and clean runs (no results at all) stay silent — unchanged.

### Part 2 — external listings excluded from orphan detection

1. `sync(...)` gains `external_titles=()` (default empty, backward-compatible). `run_ebay_sync`
   passes `external_titles=auto_cfg.get("external_titles", [])` (auto_cfg is the existing
   `business.ebay_sync` dict).
2. A helper `_is_external(listing, external_titles)` → true when any configured substring
   (case-insensitive) appears in `listing["title"]`.
3. The one orphan line becomes:

   ```python
   report["on_ebay_not_in_sheet"] = [
       dict(l) for l in listings
       if l["item_id"] not in sheet_ids and not _is_external(l, external_titles)
   ]
   ```

   Filtering here automatically drops external listings from the `_suggest_links` pool (built from
   `on_ebay_not_in_sheet`) too, so they are never suggested as a col-Q link.
4. `config/categories.yaml` under `business.ebay_sync` gains `external_titles: ["RoboTabs"]`.
   Default in code is `[]` (no exclusions) so existing runs are unchanged until configured.

Nothing else changes: the title sweep and auto-reprice already skip orphans (no sheet row), so an
external listing is never revised or repriced. The sweep SHOULD still revise an external listing
whose title says "Costco" — that is a dropshipping red flag regardless of which business it
belongs to.

## Pitfalls (do not skip)

- Match on title substring, NOT item id — item ids change on relist; titles stay recognizable.
- Case-insensitive substring, whole title match — do not anchor to word boundaries (brand names
  like "RoboTabs" may be embedded in longer titles).
- `external_titles` default `[]` means zero behavior change for anything not configured.

## Acceptance

- Full pytest suite passes on Windows (add: summary alerts on all-failed run; orphan detection
  drops a title matching an external substring and still keeps a real orphan).
- `--dry-run` on the live sheet shows the RoboTabs listing (860069578501) NO LONGER in the
  "On eBay but not in the sheet" section.
- Commit `feat: sweep failures alert + exclude external eBay listings from orphans`, push to
  `master`, reply `DONE — pushed` with test count.
