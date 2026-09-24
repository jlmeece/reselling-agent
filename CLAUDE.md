# Reselling Agent — Project Instructions

Extends the global WAT framework at `C:\Users\jorda\.claude\CLAUDE.md`. Read that first.

---

## Project Context

Costco→eBay reselling automation. Source products from Costco, research and score them, generate eBay listings, and monitor active sales.

**Orchestration (Phase 2+):** Hermes Agent running on Hostinger VPS handles scheduling and Telegram-based control.

---

## Run Modes

All modes run via `python agents/scheduler.py --mode <mode>`.

| Mode | What it does |
|------|-------------|
| `active` | Check ACTIVE listings for stock/price changes |
| `daily` | APPROVED→READY, PAUSED_OOS recheck |
| `research` | Score PENDING rows, fill tier/price/comps |
| `discovery` | Find new Costco products, add as PENDING |
| `rotation` | Score all active products, flag underperformers, send weekly digest (1x/week) |
| `refresh-notes` | Retroactively reformat Col T summary line (one-shot) |
| `recheck` | Retry Costco scrape for CHECK FAILED and empty-price rows (one-shot) |
| `audit` | Graveyard pass — remove junk, flag borderline rows (every 2 days) |
| `ebay_sync` | Sync active eBay listings (Trading API, read-only) → `units_sold` col U; flags price mismatch / ACTIVE-not-on-eBay. `--dry-run` skips the write. Runs anywhere (no Chrome) |

`python tools/ebay_export.py` is a separate standalone script (not a scheduler mode) that generates the Seller Hub CSV from READY products.

**Note:** `active` mode requires a real Chrome session (Costco cookies). Run locally only.

---

## Key Files

- `agents/scheduler.py` — entry point for all modes
- `agents/researcher.py` — full research loop: discover → scrape → score → copy
- `tools/costco_scraper.py` — Playwright CDP scraper with session refresh
- `tools/ebay_research.py` — eBay comp search (model-aware, `_sacat` scoped)
- `tools/spot_price.py` — live gold/silver/platinum via Yahoo Finance (1hr cache)
- `tools/listing_copy.py` — Claude-powered listing copy generation
- `tools/ebay_export.py` — generates Seller Hub CSV for READY products
- `tools/ebay_sync.py` — eBay Trading API `GetMyeBaySelling` → sheet sync (`fetch_active_listings`, `extract_item_id`, `sync`)
- `config/categories.yaml` — fee rates, discovery URLs, eBay category IDs, purchase limits
- `config/col_map.yaml` — Google Sheet column map (A–BA, 53 cols)
- `skills/scoring.py` — category-specific scoring (extends shared base_scoring)
- `deploy/` — VPS deployment (Docker Compose, Hermes skills, .env.template)

---

## Google Sheet

- **Sheet ID:** `1KXxULBBp4dmZb1OMGYPkf_YIE1HFd4byQCsAb-_tSic`
- **Tab:** Product Tracker, data rows 4–500
- **Windows Task Scheduler** is the scheduler; the Run Log tab records every run (not `data/run_history.json`, which is local only)

---

## Categories

- **Precious Metals** — gold/silver bars and coins. Spot-price scoring. eBay cat 3229. Priority.
- **Jewelry** — fashion rings, necklaces, earrings, bracelets. Markup scoring. eBay cat 67.
- **Outdoor Furniture** — implemented
- **Watches** — implemented
- **Pharmacy** — implemented
- **Small Appliances** — implemented
- **Toys** — unbuilt stub (`config/categories.yaml` has the key but `discovery_urls: []`, not yet active)

---

## Constraints & Known Issues

- Costco scraper blocks after ~14 product pages — session refresh runs every 20 products
- YouTube API quota exhausts daily — Reddit/DDG fallback handles it automatically
- Active monitor must run locally (Chrome CDP) — pre-scans for active/approved rows and skips the browser launch entirely if there's nothing to check
- Google Sheets caps writes at 60/min per service account — route every `.execute()` write through `tools.sheet_writer.execute_with_retry` (retries 429/500/503/socket.timeout, 1/2/4/8/16s backoff); auditor sleeps 1.5s per row delete, 0.3s per flag write
- Non-idempotent Sheets writes (`deleteDimension`, `addSheet`) must pass `retry_statuses=(429,), retry_timeouts=False` to `execute_with_retry` — a 5xx/timeout may have landed, and a retry would delete the next row or fail on a duplicate tab. `telegram_bot.py`'s deleteDimension is still unwrapped
- `data/run_history.json` is local only — cloud runs write to Sheet Run Log tab instead

---

## Recent Changes

- eBay Taxonomy API for item specifics (`tools/ebay_taxonomy.py`, `tests/test_ebay_taxonomy.py`): `get_item_aspects(cat_id)` returns the REQUIRED aspects (`aspectConstraint.aspectRequired`; also accepts `requirement: REQUIRED`) via OAuth client-credentials (EBAY_APP_ID/EBAY_CERT_ID, scope `oauth/api_scope`, no EBAY_AUTH_TOKEN). Contract: **list = authoritative (`[]` = nothing required), `None` = unavailable → caller falls back**. Cache `data/ebay_taxonomy_cache.json` (gitignored; 30d, bad/non-leaf ID cached negatively 1d, 60s cooldown after transient failures so an outage can't stall an export). `ebay_export` calls it per row after category resolution: required list = Taxonomy ∪ yaml `ebay_required_specifics` (`YAML_FLOOR = True`; live check found the API requires far less than the yaml for most Pharmacy/appliance IDs and we can't tell which yaml entries came from real 21919303 rejections — flip to False once drift is reviewed). Aspects not in `_EBAY_COLUMNS` get dynamic `C:` columns before ShippingProfileName; SELECTION_ONLY values are matched case-insensitively to the allowed list (else "Other", else "Does Not Apply"); optional per-category `ebay_aspect_defaults` in categories.yaml. `python tools/ebay_taxonomy.py --check` = validity + drift report for every ID in categories.yaml (**run 2026-09-23: 39482, 45109, 45108, 137835, 10968, 261940, 20667, 30063, 116174 are not valid leaf IDs in eBay US — they fall back to yaml; review before relying on them**; 11892/11894 are valid). `tests/conftest.py` autouse fixture blocks `_http_json` and redirects the cache for every test
- eBay category suggestions replace the Serper lookup (`get_category_suggestions(title)` in `tools/ebay_taxonomy.py`; `_serper_category_lookup` and its cache deleted from `ebay_export`): `_suggested_category_id` takes the top 3 suggestions and returns the first whose `get_item_aspects` loads, so specifics come from the SAME category that is written; anything else (API down, no match, none valid) keeps the yaml ID. `SUGGESTIONS_PRIMARY = True` in ebay_export.py is the rollback switch (False = yaml IDs only). Suggestions are cached per title (`q:<title>` keys in `data/ebay_taxonomy_cache.json`; 30d, empty result 1d). **Suggestions stay in eBay's relevance order — do NOT sort by `categoryTreeNodeLevel`**: every suggestion is already a leaf, and deepest-first picked junk (air fryer → commercial deep fryers, vitamin D → sleeping pills, watch → watch dials); `level` is informational only. `--check` now prints yaml ID vs top suggestion per category keyword using real titles from `data/knowledge/products` (rows with a made-up keyword title are marked synthetic = weak signal). **Drift found 2026-09-23 (categories.yaml NOT yet edited — review):** stale → suggested: 39482 gold bar→178906 (Gold Bars & Rounds), gold coin→177652, 45109 silver bar→39489, 45108 silver coin→177653, 137835 bracelet→261988, 10968 ring→(rings are 261994; the yaml has 261994 mislabelled as earrings — earrings are 261990), 261940→139849 (Patio & Garden Furniture Sets), 20667→per-product (rice cooker 122932 etc.), 30063 coffee→184665, 116174 stand mixer→133701 (Countertop Mixers). Valid-but-different: 14070 is used for blender/vitamix/food processor but eBay says 133704 Countertop Blenders / 20673 Food Processors; 137839→261993 Necklaces & Pendants (both valid); Pharmacy vitamins/fish oil/calcium all → 11776 Vitamins & Minerals vs the yaml 183904/11892/11894. With primary on, these valid-but-different rows WILL be overridden at export
- eBay sync Stage 1 (`tools/ebay_sync.py`, `--mode ebay_sync [--dry-run]`, `tests/test_ebay_sync.py`): read-only on eBay, flag-only on the sheet — **col U `units_sold` is the only write** (via `safe_write_row`, only when QuantitySold changed; titles re-verified first so auditor row shifts can't misdirect a write). Matches by item ID parsed from col Q. Reports `price_mismatch`, `active_not_on_ebay`, `on_ebay_not_in_sheet` to the Run Log notes; Telegram only for price_mismatch / active_not_on_ebay / a rejected auth token (error 931/932/16110). Blank `EBAY_AUTH_TOKEN` = logged skip, no crash. An API failure returns `[]` and never computes `active_not_on_ebay` (an outage must not flag every ACTIVE row). **Post dry-run fixes:** eBay's ActiveList sends no view count, so `view_count` is `None` (unknown, not 0) and isn't reported; `watch_count` stays 0 when the tag is absent (field name unconfirmed until a listing shows >0 watchers). ACTIVE rows with blank col Q now read "no URL in col Q" (not "never listed") and, when an unclaimed eBay listing's title matches (exact, or token-Jaccard ≥0.8 = "possibly"; ambiguous = no suggestion), add "likely = eBay item <ID>, fill col Q" — suggestion only, col Q is never written; Run Log notes get `link_suggestions N`. Identical Telegram alerts are suppressed for 24h (`_dedupe_alert`, state `data/.ebay_sync_alert.json`, gitignored; a clean run clears it; dry-run skips it; a failed send stays suppressed until the window ends). **Stage-1 gap:** ActiveList drops fully sold-out listings, so their final sale isn't written to col U — they surface as `active_not_on_ebay`; Stage 2 = SoldList. `safe_write_row`/`PROTECTED_COLS` moved from `telegram_bot.py` to `tools/sheet_writer.py` (bot re-imports them). `tools/register_ebay_sync_task.ps1` registers `WAT-EbaySync` (2h) — **not yet registered**; never smoke-tested against the live eBay API
- Bot "📦 Mark Listed": READY cards (Search, search:pick, single-match `/lookup`) get a button → prompt for eBay ID/URL (or Skip) → confirm → `safe_write_row` status ACTIVE + platform (col E) "eBay" (+ col Q if given; a bare ID is stored as `https://www.ebay.com/itm/<ID>`). Confirm re-reads the row and refuses unless it is still the same READY title (row numbers shift when the auditor deletes). State lives in `user_data["awaiting_listing"]`/`["pending_listed"]`, cleared by `_clear_listing_state` on navigation. Tests: `tests/test_mark_listed.py`. Needs a bot `/restart`; never smoke-tested against live Telegram
- Run-lock file (`data/.scheduler_lock`) prevents overlapping scheduler runs (45-min staleness before it's reclaimed)
- Cookie auto-refresh (24h throttle, `tools/cookie_refresh.py`) triggers on file age ≥25d **or** >20% cookies expired (scheduler `_check_cookie_age` and scraper `_load_cookies`)
- Telegram alerts fire on scheduler crash and on expired/aging Costco cookies
- Dashboard shows per-category average margins and MPT (Sharpe-ratio) rotation ranking
- Bot liveness + watchdog: the bot touches `data/.telegram_bot.alive` every 60s from its asyncio loop, only while polling is running. `watchdog.ps1` (WAT-Watchdog task, every 5 min; register via `tools/register_watchdog_task.ps1`) starts the bot if dead, and if the file is stale >10 min force-kills **only python.exe** (the `.bat` loop relaunches it) and sends a Telegram alert. Never kill the cmd loop; deploy bot code before changing the watchdog. Test with `.\watchdog.ps1 -DryRun -StaleMinutes 0 -Verbose`
- Sheet grid guard: `researcher.py` calls `ensure_grid_columns` at start so the tab always has ≥ `required_grid_columns()` (53, from col_map.yaml). Before this (2026-09-18 → 09-23) MPT cols AX–BA exceeded the 49-col grid and research exited 1. `sheet_formatter` no longer shrinks the grid below that. MPT columns AX–BA (and AW) now have header labels in `HEADER_LABELS` (`TOTAL_COLS`=53); `sheet_formatter.refresh_header_row()` rewrites just row 3 on the live sheet without a full dashboard rebuild. AW–BA stay visible (`HIDDEN_END`=48)
- Auditor (`_remove_reason`): SCORED rows with net in [$1, $4) and ≥30 days since checked are removed ("Below $4 review floor, stale N days") — `SCORED_STALE_NET_CEILING` is pinned by a test to `_REVIEW_MIN_NET_PROFIT`. A **blank net cell means un-priced (None), not $0.00**: economics rules skip it (2026-09-23 the old `_safe_float` default deleted 133 fresh PENDING rows as "Below floor — net $0.00"; they are in the Graveyard tab)
- Review queue skips items with net profit < $4 (`_REVIEW_MIN_NET_PROFIT`); they stay SCORED. Card labels: "Net after ad reserve", "Flag for review"
- Bot heartbeat: `agents/telegram_bot.py` runs a daemon thread that pings `HEALTHCHECK_URL_BOT` at startup then every 300s (unset = disabled, one log line). Set healthchecks.io period 5 min / grace ≥10 min. Proves the process is alive, not that polling is
- eBay export (`tools/ebay_export.py`): quantity = purchase limit from col W (`N/day`) else `DEFAULT_QUANTITY` (99). Pharmacy category 11896 → 183904 (`_CATEGORY_MIGRATIONS` also remaps Serper/cache hits). Fish oil/omega (11892) and calcium (11894) are still in the legacy 118xx range and unconfirmed: `UNVERIFIED_CATEGORY_IDS` logs a "verify on next live upload" warning at export — once a live upload settles them, add the real ID to `_CATEGORY_MIGRATIONS`. Every key in a category's `ebay_required_specifics` (categories.yaml) is guaranteed non-blank (eBay error 21919303): real value → title inference → `default_dimensions` / "Does Not Apply" / "Unbranded". Researcher now writes a `Costco specs: Brand | Model | Dimensions` line into notes (col AV) from `scrape_costco` (`parse_dimensions`); rows researched before this use category default dimensions until re-researched. `_parse_brand_from_notes` now needs an explicit `Brand:` (it used to read "Brand Builder" lens text as brand "Builder")
