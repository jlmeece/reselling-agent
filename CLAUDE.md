# Reselling Agent — Project Instructions

Extends the global WAT framework at `C:\Users\jorda\.claude\CLAUDE.md`. Read that first.

---

## Project Context

Costco→eBay reselling automation. Source products from Costco, research and score them, generate eBay listings, and monitor active sales.

**Orchestration (Phase 2+):** Hermes Agent running on Hostinger VPS handles scheduling and Telegram-based control. GitHub Actions remains as a fallback.

---

## Run Modes

| Mode | Command | What it does |
|------|---------|-------------|
| `discovery` | `python agents/scheduler.py --mode discovery` | Find new Costco products, add as PENDING |
| `research` | `python agents/scheduler.py --mode research` | Score PENDING rows, fill tier/price/comps |
| `daily` | `python agents/scheduler.py --mode daily` | APPROVED→READY, PAUSED_OOS recheck |
| `active` | `python agents/scheduler.py --mode active` | Check ACTIVE listings for stock/price changes |
| `rotation` | `python agents/scheduler.py --mode rotation` | Weekly digest — score all ACTIVE products |
| `export` | `python tools/ebay_export.py` | Generate Seller Hub CSV from READY products |

**Note:** `active` mode requires a real Chrome session (Costco cookies). Run locally only — not compatible with CI.

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
- `config/col_map.yaml` — Google Sheet column map (A–AV, 48 cols)
- `skills/scoring.py` — category-specific scoring (extends shared base_scoring)
- `deploy/` — VPS deployment (Docker Compose, Hermes skills, .env.template)

---

## Google Sheet

- **Sheet ID:** `1KXxULBBp4dmZb1OMGYPkf_YIE1HFd4byQCsAb-_tSic`
- **Tab:** Product Tracker, data rows 4–500
- **Run Log tab** is the source of truth for GitHub Actions run history (not `data/run_history.json`)

---

## Categories

- **Precious Metals** — gold/silver bars and coins. Spot-price scoring. eBay cat 3229. Priority.
- **Jewelry** — fashion rings, necklaces, earrings, bracelets. Markup scoring. eBay cat 67.
- **Outdoor Furniture** — future
- **Watches** — future

---

## Constraints & Known Issues

- Costco scraper blocks after ~14 product pages — session refresh runs every 20 products
- YouTube API quota exhausts daily — Reddit/DDG fallback handles it automatically
- Active monitor must run locally (Chrome CDP) — GitHub Actions guard prevents crash
- `data/run_history.json` is local only — cloud runs write to Sheet Run Log tab instead
