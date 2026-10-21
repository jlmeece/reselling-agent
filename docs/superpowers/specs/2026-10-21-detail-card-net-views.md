# Detail card: drop ad-reserve net lines + add live views/watchers (Oct 21 2026)

## Goal

Two detail-card changes:

1. **Declutter net.** The card shows three net-related lines ("Profit reserve", "Net before
   reserve", "Net after reserve"). Jay isn't running ads — collapse to one base-net line.
2. **Add engagement signal.** Show the listing's live **views + watchers** so Jay can decide
   reprice-vs-hold-vs-end without guessing. Views aren't in the bulk sync, so fetch them
   on-demand (one GetItem per card open).

## Rule

- Net: one line — `Net $X (Y%)` (the base net, no ad reserve). The `ad_budget` (col AH) value stays
  in the sheet, just not rendered.
- Engagement: `👁️ N views · ⭐ N watchers`, fetched live via `fetch_item_price` (GetItem) when the
  card opens. A failed/unknown fetch simply omits the line — the card must never block or error on it.

## Changes

### 1. `agents/telegram_bot.py` — `format_product_detail` (net cleanup)

Replace the three lines currently rendered (the `ad_budget` "Profit reserve" line, the
`label="Net before reserve:"` net line, and the `_format_net_with_ads_line(...)` "Net after
reserve" line) with a single:

```python
lines.append(_format_net_fragment(p.get("net_profit"), p.get("net_margin"), label="Net:"))
```

- `_format_net_with_ads_line` becomes unused — leave it defined (harmless, may have tests) or
  remove it + its tests; either is fine, just keep the suite green.

### 2. `tools/ebay_sync.py` — return views + watchers from GetItem

- `_build_get_item_xml`: add `"Item.HitCount", "Item.WatchCount"` to the `OutputSelector` tuple.
- `fetch_item_price`: add `view_count` and `watch_count` to the returned dict:
  - `out["view_count"] = _num(_text(item, "HitCount"), int, None)`  (None = unknown → omit)
  - `out["watch_count"] = _num(_text(item, "WatchCount"), int, 0)`  (absent tag = 0 watchers,
    matching the ActiveList convention)
  - Initialize both to `None` in the `out = {...}` dict so callers can `.get()` them on a failed
    call. Update the docstring's return list.

### 3. `agents/telegram_bot.py` — `cb_activelist_pick` (fetch + show engagement)

After `item_id = extract_item_id(p.get("ebay_listing_url"))` (and before the scheduled-action
line), when `item_id` is truthy:

```python
if item_id:
    try:
        live = await asyncio.to_thread(fetch_item_price, item_id)
    except Exception as e:            # never let a views fetch break the card
        logger.warning(f"activelist:pick views fetch failed: {e}")
        live = {}
    parts = []
    if live.get("view_count") is not None:
        parts.append(f"👁️ {live['view_count']} views")
    if live.get("watch_count") is not None:
        parts.append(f"⭐ {live['watch_count']} watchers")
    if parts:
        lines.append(" · ".join(parts))
```

`fetch_item_price` is already imported; `asyncio` is already imported.

## Tests

- `test_telegram_bot.py`: update any test asserting the "Profit reserve" / "Net before reserve" /
  "Net after reserve" strings on the card to assert a single `Net: ` line instead.
- `test_ebay_sync.py`: `fetch_item_price` parses `HitCount`→`view_count` and `WatchCount`→
  `watch_count` from a GetItem fixture; absent tags → `None`/`0`.
- New/updated bot test: `cb_activelist_pick` renders `👁️ N views · ⭐ N watchers` when the
  (patched) fetch returns them, and renders neither when the fetch fails or returns `None`.

## Acceptance

- Run the curated safe suite. Commit message:
  `feat: detail card shows base net + live views/watchers (drop ad-reserve lines)`.
- Push to `master`, reply `DONE — pushed`.
