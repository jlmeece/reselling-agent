# OAuth user token: live view counts from Sell Analytics (Oct 21 2026)

## Goal

Jay now has an eBay OAuth **user** refresh token (`EBAY_OAUTH_REFRESH_TOKEN` in `.env`). Use it to
fetch per-listing **view counts** from eBay's Sell Analytics API and show them on the detail card
(the current `👁️ views` never worked because GetItem/Trading API no longer returns view counts).

## Rule

- eBay aggregates views **daily** (Seller Hub traffic data). Fetch once per ~24h into a local cache,
  not per card-open — the traffic-report endpoint returns ALL listings in one call, so bulk-once is
  both cheaper and correct.
- Display: one engagement line `👁️ N views (30d) · ⭐ N watchers`. Views come from the cache; watchers
  still come from the existing GetItem fetch. Omit either piece when its value is unknown/missing.
- **Fees (Sell Finances) is a SEPARATE follow-up spec** — do not build it here.

## Changes

### 1. `tools/ebay_analytics.py` (new)

- `get_user_access_token(force=False) -> str | None`:
  - Reads `EBAY_OAUTH_REFRESH_TOKEN`, `EBAY_APP_ID`, `EBAY_CERT_ID` from env (same `.env` loader as
    the rest of the tool).
  - POST `https://api.ebay.com/identity/v1/oauth2/token`, form body `grant_type=refresh_token` +
    `refresh_token=<token>` + `scope=<the two scopes, space-separated>`, Basic auth
    `base64(APP_ID:CERT_ID)` (reuse the same pattern as `tools/ebay_taxonomy.py::_get_oauth_token`).
  - Cache the returned access_token + `expires_at` (now + `expires_in` − 60s skew) in a gitignored
    file `data/.ebay_user_token.json`; reuse until near expiry, `force=True` bypasses the cache.
  - Returns None on any failure (missing token, bad response, non-200) — never raises.
- `get_listing_views(days=30) -> dict[str, int] | None`:
  - Get an access token, then GET
    `https://apiz.ebay.com/sell/analytics/v1/traffic_report` with `Authorization: Bearer <token>`,
    query `dimension=ITEM`, `metric=LISTING_VIEWS_TOTAL`, and `filter` for `marketplace_ids:{EBAY_US}`
    + `date_range:[<YYYYMMDD>..<YYYYMMDD>]` (last `days` days). Verify the exact `filter` syntax and
    the metric name against a live call — eBay's date-range filter format is fiddly; the smoke test
    below is the source of truth.
  - Parse the response rows (`dimensionValues[0].value` = item id, `metricValues[0].value` = views)
    into `{item_id: int}`. Return None on any failure (token, auth/scope, empty, parse).

### 2. Fetch + cache during `ebay_sync`

- In `run_ebay_sync` (or the `ebay_sync` orchestrator in `agents/scheduler.py`), after the existing
  sync, fetch views once per ~24h: read `data/listing_views.json` `{fetched_at, views}`, and only
  re-fetch when `fetched_at` is missing or older than 24h. Write atomically. On fetch failure, keep
  the stale cache (log it) — views are a nice-to-have, never a reason to fail the sync.
- Do NOT add a column; the cache file is the store.

### 3. `agents/telegram_bot.py` — `cb_activelist_pick` engagement line

- After the existing watchers fetch, also read the cached views for `item_id` from
  `data/listing_views.json`, and render `👁️ N views (30d)` as the first part of the engagement line
  when present (watchers stays second). Omit the views part when the cache has no entry or the file
  is missing/unreadable.

### 4. `.env.template`

- Add a documented `EBAY_OAUTH_REFRESH_TOKEN=` under the eBay Trading API block, with a one-line
  note: user OAuth token for Sell Analytics (views) / Sell Finances (fees), ~18-month refresh token.

## Tests

- `tools/ebay_analytics.py`: token request uses Basic auth + `grant_type=refresh_token` (mock the
  HTTP layer, mirror `test_ebay_taxonomy.py`'s pattern); access token cached until near expiry then
  refreshed; `get_listing_views` parses rows → `{item_id: views}` and returns None on bad response.
- `cb_activelist_pick`: renders `👁️ N views (30d) · ⭐ N watchers` when both are present, `⭐ N
  watchers` alone when the view cache is missing.
- Keep the full safe suite green.

## Acceptance

- **Live smoke test (required, the token may be mangled):** run the new analytics fetch once against
  real eBay and confirm it returns non-None view counts. If the token errors (401/scope), report the
  exact eBay error — that means the refresh token or the `sell.analytics.readonly` scope is off.
- Run the curated safe suite. Commit message:
  `feat: OAuth user token — live view counts (Sell Analytics, 30d) on the detail card`.
- Push to `master`, reply `DONE — pushed` with the smoke-test result (view counts returned, or the
  exact error).
