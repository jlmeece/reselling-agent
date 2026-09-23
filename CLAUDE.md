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

- Run-lock file (`data/.scheduler_lock`) prevents overlapping scheduler runs (45-min staleness before it's reclaimed)
- Cookie auto-refresh (24h throttle, `tools/cookie_refresh.py`) triggers on file age ≥25d **or** >20% cookies expired (scheduler `_check_cookie_age` and scraper `_load_cookies`)
- Telegram alerts fire on scheduler crash and on expired/aging Costco cookies
- Dashboard shows per-category average margins and MPT (Sharpe-ratio) rotation ranking
