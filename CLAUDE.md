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

- eBay sync Stage 1 (`tools/ebay_sync.py`, `--mode ebay_sync [--dry-run]`, `tests/test_ebay_sync.py`): read-only on eBay, flag-only on the sheet — **col U `units_sold` is the only write** (via `safe_write_row`, only when QuantitySold changed; titles re-verified first so auditor row shifts can't misdirect a write). Matches by item ID parsed from col Q. Reports `price_mismatch`, `active_not_on_ebay`, `on_ebay_not_in_sheet` to the Run Log notes; Telegram only for price_mismatch / active_not_on_ebay / a rejected auth token (error 931/932/16110). Blank `EBAY_AUTH_TOKEN` = logged skip, no crash. An API failure returns `[]` and never computes `active_not_on_ebay` (an outage must not flag every ACTIVE row). **Stage-1 gap:** ActiveList drops fully sold-out listings, so their final sale isn't written to col U — they surface as `active_not_on_ebay`; Stage 2 = SoldList. `safe_write_row`/`PROTECTED_COLS` moved from `telegram_bot.py` to `tools/sheet_writer.py` (bot re-imports them). `tools/register_ebay_sync_task.ps1` registers `WAT-EbaySync` (2h) — **not yet registered**; never smoke-tested against the live eBay API
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
