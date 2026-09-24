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
| `ebay_sync` | Sync active eBay listings (Trading API, read-only) → `units_sold` col U; flags margin breaches (net<0) / ACTIVE-not-on-eBay. `--dry-run` skips the write. Runs anywhere (no Chrome) |

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
- `config/col_map.yaml` — Google Sheet column map (A–BB, 54 cols; BB `buy_cost` is manual)
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

- Sale-aware active monitor (Phase 2, 2026-09-24; `tools/sale_monitor.py`, `tests/test_sale_monitor.py`, `tests/test_run_active_monitor.py`): `run_active_monitor` classifies each cost change via `classify_cost_event`. **Sale start** (cost dropped >$0.50 AND `on_sale`) writes col G, AW (regular), X (badge `🔥 -$8 ends 10/18/26`), notes `on sale — margin +$X`, logs Sale History, and does NOT touch col P or alert (keep the eBay price, pocket the margin). **Sale end / cost rise** (rise >$0.50, or badge gone + any rise) writes G, clears X/AW, sets col P `YES — update listing` and sends an URGENT alert `sale ended — cost $A→$B, reprice eBay to $<suggest_reprice> to keep margin`; if the eBay price already >= the target it is quiet (note only). Drift = col G only. A badge that vanished with no cost rise is cleared silently (col X can hold DOM false-positives); a scrape with no price never triggers an event. **`sale_expires` now comes from the price API** (`_promo_end_local` in `costco_scraper.py`: `discounts[].promotions[].promotionEndDate` UTC → Pacific `M/D/YY`); before, only a DOM regex filled it, which is why the Sep 23 countdown never fired. The expiry check now reads col X from the in-memory row (updated this run), parses year-less dates too, uses `SALE_URGENT_HOURS` (48h warn / 24h urgent tiers), and dedups per title+end+tier in `data/.sale_expiry_alert.json` (gitignored); its reprice target uses `suggest_reprice`. Col P is no longer blanked on every quiet run — a `YES` flag persists until eBay price (col H) >= reprice target. New `python agents/scheduler.py --mode active --row N` checks a single sheet row (also limits the expiry alerts). Sheet read range is now A:AW. **Live-verified on Energy Shot (row 9):** G 39.99→31.99, AW 39.99, X `ends 10/18/26`, no P, no alert; the Sale History tab now exists. Not yet live-exercised: the sale-END path (unit/mocked tests only)
- Costco price scrape fix (2026-09-24, `_parse_price_payload` in `tools/costco_scraper.py`, `tests/test_costco_price_parse.py`, real fixtures in `tests/fixtures/`): after the redesign the price API is `gdx-api.costco.com/catalog/product/dispprice-api/v3/display-price-lite?whsNumber=847,1&item=<itemNo>` and `priceData` / `displayPrice` are now LISTS, one entry per warehouse (`warehouseNumber`): `onlinePrice` = regular price, `aggregatedDiscountAmt` = instant savings, `deliveredPrice` = price you pay (= online - discount); `discounts[].promotions[].promotionEndDate` also present. The old parse returned nothing, so col G froze. **Warehouse choice matters:** the entry is picked by the URL's `whsNumber` order (store first — 847, what the page shows and what col G always held; `1` = Costco.com, often a different price, e.g. 34.99 vs 31.99). Price, `original_price` and `sale_savings` now all come from that one response (`authoritative` = it carried discount fields; then the DOM sale-text patterns are skipped — they false-positived a $100 sale on a $19.99 vitamin from other products' banners, so some existing col X/AW values may be bogus until re-researched). `item_number` now = API item id. Costco price-API misses are counted per process; the scheduler sends ONE Telegram alert per run if >=3 pages and >=20% got no API price. NOTE: the Energy Shot in the sheet is 4000099948 (item 1711796, $39.99 -> $31.99); 4000100002 is the different Extra Strength item (1711799, $43.99 -> $35.99)
- Phase 1 (2026-09-24): **margin alerts replace price-mismatch noise** in `tools/ebay_sync.py` — `margin_flag(price, cost_basis, fee_rate, ship, ad, sold_90d)` -> hard (net<0, Telegram per run, includes break-even price) / soft (0<=net<$4 AND sold_90d==0, once-a-day digest via `result["digest"]`, state `data/.ebay_sync_digest.json`) / None. cost_basis = col BB `buy_cost` (MANUAL, Jay pastes what he paid) else col G; unknown price/cost/fee = never judged (counted `margin_unchecked` in Run Log notes), blank sold_90d = unknown not 0. `report["price_mismatch"]` -> `report["margin_breach"]`. Reads A:BB; `run_ebay_sync` calls `ensure_grid_columns` (grid now 54). Live dry run flagged a real one (Energy Shot row 9, eBay $41.48 vs cost $39.99 = -$4.01). **`--dry-run` still SENDS Telegram alerts** (it only skips sheet writes and de-dup state). **Sale History tab** (`tools/sale_history.py`, `tests/test_sale_history.py`): `log_sale()` appends [title, category, scrape_date, sale_price, regular_price, sale_end_date] when col X is non-blank, skipping a repeat of the same title+price within 7 days; never raises; called from `researcher.py` (after the col AW write) and `scheduler.py` recheck mode. Tab is created on first sale, not yet present live
- Costco image scraping fixed after the Sep 2026 page redesign (`_extract_image_urls` in `tools/costco_scraper.py`, `tests/test_costco_image_extraction.py`): images now come from `gdx-assets.costco.com` (Adobe AEM; JPEG bytes behind a `.avif` filename) in Material-UI markup with no stable class. Order: `img[alt^='Enlarge Product Preview']` → CDN hostname (gdx-assets/channeladvisor/costco-static) → legacy container classes → `og:image`. Capped at 5 (live gallery had 17 imgs, likely across color variants); a zero-image scrape logs a warning. **Open questions:** eBay may reject `.avif`-named PicURLs (unverified — check on next live upload); rows researched between the redesign and this fix have blank col AT/AH images until re-researched
- eBay Taxonomy API for item specifics (`tools/ebay_taxonomy.py`, `tests/test_ebay_taxonomy.py`): `get_item_aspects(cat_id)` returns the REQUIRED aspects (`aspectConstraint.aspectRequired`; also accepts `requirement: REQUIRED`) via OAuth client-credentials (EBAY_APP_ID/EBAY_CERT_ID, scope `oauth/api_scope`, no EBAY_AUTH_TOKEN). Contract: **list = authoritative (`[]` = nothing required), `None` = unavailable → caller falls back**. Cache `data/ebay_taxonomy_cache.json` (gitignored; 30d, bad/non-leaf ID cached negatively 1d, 60s cooldown after transient failures so an outage can't stall an export). `ebay_export` calls it per row after category resolution: required list = Taxonomy ∪ yaml `ebay_required_specifics` (`YAML_FLOOR = True`; live check found the API requires far less than the yaml for most Pharmacy/appliance IDs and we can't tell which yaml entries came from real 21919303 rejections — flip to False once drift is reviewed). Aspects not in `_EBAY_COLUMNS` get dynamic `C:` columns before ShippingProfileName; SELECTION_ONLY values are matched case-insensitively to the allowed list (else "Other", else "Does Not Apply"); optional per-category `ebay_aspect_defaults` in categories.yaml. `python tools/ebay_taxonomy.py --check` = validity + drift report for every ID in categories.yaml (**run 2026-09-23: 39482, 45109, 45108, 137835, 10968, 261940, 20667, 30063, 116174 are not valid leaf IDs in eBay US — they fall back to yaml; review before relying on them**; 11892/11894 are valid). `tests/conftest.py` autouse fixture blocks `_http_json` and redirects the cache for every test
- eBay category suggestions replace the Serper lookup (`get_category_suggestions(title)` in `tools/ebay_taxonomy.py`; `_serper_category_lookup` and its cache deleted from `ebay_export`): `_suggested_category_id` takes the top 3 suggestions and returns the first whose `get_item_aspects` loads, so specifics come from the SAME category that is written; anything else (API down, no match, none valid) keeps the yaml ID. `SUGGESTIONS_MODE` in ebay_export.py: `"invalid_only"` (default — a suggestion is used only when eBay says the yaml ID isn't a valid leaf or the yaml has none; an unreachable API keeps the yaml ID, via `ebay_taxonomy.category_status` = valid/invalid/unknown), `"always"` (suggestion overrides every row), `"off"` (yaml only). Suggestions are cached per title (`q:<title>` keys in `data/ebay_taxonomy_cache.json`; 30d, empty result 1d). **Suggestions stay in eBay's relevance order — do NOT sort by `categoryTreeNodeLevel`**: every suggestion is already a leaf, and deepest-first picked junk (air fryer → commercial deep fryers, vitamin D → sleeping pills, watch → watch dials); `level` is informational only. `--check` now prints yaml ID vs top suggestion per category keyword using real titles from `data/knowledge/products` (rows with a made-up keyword title are marked synthetic = weak signal). **Drift found 2026-09-23 — the invalid IDs below were FIXED in categories.yaml (verified live; gold/silver bar+coin → 178906/177652/39489/177653, bracelet + jewelry default → 261988, earring → 261990 (261994 is Rings), outdoor sets + default → 139849, coffee → 184665, stand mixer → 133701). Still TODO (unresolvable without a real title, left as-is with TODO comments): Jewelry ring 10968 (--check says Rings = 261994 but no ring product exists to confirm) and Small Appliances default 20667 (parent; resolved per title by suggestions in invalid_only mode). Original findings:** stale → suggested: 39482 gold bar→178906 (Gold Bars & Rounds), gold coin→177652, 45109 silver bar→39489, 45108 silver coin→177653, 137835 bracelet→261988, 10968 ring→(rings are 261994; the yaml has 261994 mislabelled as earrings — earrings are 261990), 261940→139849 (Patio & Garden Furniture Sets), 20667→per-product (rice cooker 122932 etc.), 30063 coffee→184665, 116174 stand mixer→133701 (Countertop Mixers). Valid-but-different: 14070 is used for blender/vitamix/food processor but eBay says 133704 Countertop Blenders / 20673 Food Processors; 137839→261993 Necklaces & Pendants (both valid); Pharmacy vitamins/fish oil/calcium all → 11776 Vitamins & Minerals vs the yaml 183904/11892/11894. These valid-but-different rows are a SEPARATE REVIEW — unchanged in the yaml, and invalid_only mode leaves them alone (only `always` would override them)
- eBay sync Stage 1 (`tools/ebay_sync.py`, `--mode ebay_sync [--dry-run]`, `tests/test_ebay_sync.py`): read-only on eBay, flag-only on the sheet — **col U `units_sold` is the only write** (via `safe_write_row`, only when QuantitySold changed; titles re-verified first so auditor row shifts can't misdirect a write). Matches by item ID parsed from col Q. Reports `price_mismatch`, `active_not_on_ebay`, `on_ebay_not_in_sheet` to the Run Log notes; Telegram only for price_mismatch / active_not_on_ebay / a rejected auth token (error 931/932/16110). Blank `EBAY_AUTH_TOKEN` = logged skip, no crash. An API failure returns `[]` and never computes `active_not_on_ebay` (an outage must not flag every ACTIVE row). **Post dry-run fixes:** eBay's ActiveList sends no view count, so `view_count` is `None` (unknown, not 0) and isn't reported; `watch_count` stays 0 when the tag is absent (field name unconfirmed until a listing shows >0 watchers). ACTIVE rows with blank col Q now read "no URL in col Q" (not "never listed") and, when an unclaimed eBay listing's title matches (exact, or token-Jaccard ≥0.8 = "possibly"; ambiguous = no suggestion), add "likely = eBay item <ID>, fill col Q" — suggestion only, col Q is never written; Run Log notes get `link_suggestions N`. Identical Telegram alerts are suppressed for 24h (`_dedupe_alert`, state `data/.ebay_sync_alert.json`, gitignored; a clean run clears it; dry-run skips it; a failed send stays suppressed until the window ends). **Stage-1 gap:** ActiveList drops fully sold-out listings, so their final sale isn't written to col U — they surface as `active_not_on_ebay`; Stage 2 = SoldList. `safe_write_row`/`PROTECTED_COLS` moved from `telegram_bot.py` to `tools/sheet_writer.py` (bot re-imports them). `tools/register_ebay_sync_task.ps1` registers `WAT-EbaySync` — **registered and live** (first 2h repeat 2026-09-23 23:08; re-registered 2026-09-24 as 4 daily triggers 10:10/14:10/18:10/22:10, the :10 offset because a run that hits the scheduler lock is skipped, not queued); live-tested against the eBay API (10 active listings fetched, Run Log rows written)
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
