# Views on the Active List — inline count + sort (Oct 21 2026)

## Goal

Views are the traction signal; surface them on the Active List so high-view listings can be found
without opening every card. Show the 30-day view count inline on each line and add a sort toggle:
**Net ▾ (default) / Views ▾**.

## Rule

- Views come from the existing cache `data/listing_views.json` (`tools.ebay_analytics.load_views_cache()`
  / `cached_views(item_id)`), refreshed ~daily by `ebay_sync`. No new eBay call from the list.
- Sort toggle flips between `net` (current `_active_net_key`, net desc) and `views` (views desc;
  listings with no cached view count sort last). The toggle re-renders the current page.

## Changes — `agents/telegram_bot.py`

### 1. `cb_activelist_show` — sort state + view-aware ordering

- Extend the page `arg` to also carry sort, or add a separate `activelist:sort:<net|views>` callback
  (either is fine; keep `activelist:show` and `activelist:show:<page>` working as "net sort").
- Load the views cache once (`load_views_cache()`); build a per-item views lookup keyed by the item
  id parsed from col Q.
- Sort: when `net`, `items.sort(key=_active_net_key)` (unchanged); when `views`, sort by
  `views.get(item_id, -1)` descending (unknown = last), tie-break by `_active_net_key`.
- Add a toggle button in the footer: `↕️ Sort: Views` when currently net (→ views), `↕️ Sort: Net`
  when currently views (→ net). Keep Prev/Next/Refresh/Dashboard.

### 2. `_format_active_line` — inline views

- Accept the cached view count (or a lookup) and append `· 👁️ {views}` to the line when the item has
  a cached count. Callers that don't pass views keep the current line unchanged.

## Tests

- Sort: with a mixed set (some items cached, some not), `views` sort puts the highest-view item first
  and unknown-view items last; `net` sort is unchanged.
- `_format_active_line` appends `👁️ N` only when a view count is supplied.
- The toggle button flips label + callback correctly; `activelist:show` with no sort arg still means
  net sort (back-compat).

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: Active List shows 30-day views inline and sorts by views or net`.
- Push to `master`, reply `DONE — pushed`.
