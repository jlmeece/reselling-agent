# 🛒 The Reselling Engine · Full Guide

Costco → eBay drop-shipping, automated end to end. It finds products, scores them, writes the listing, builds your branded photo, then monitors and protects everything that goes live.

## The Pipeline

1. **PENDING** · discovery finds new Costco products and parks them here
2. **Research** scores each one → **SCORED** (Tier 1), **WATCH** (Tier 2), or **PAUSED_DEMAND** (Tier 3)
3. You review SCORED and approve → **APPROVED**
4. **Daily run** (1x/day) verifies Costco stock and generates listing copy → APPROVED → **READY**
5. You run `ebay_export.py` → generates the Seller Hub CSV from all READY rows (read-only, no status change)
6. You upload the CSV to eBay Seller Hub → listings go live
7. You paste the live listing URL into column Q ← the one manual step
8. **Active monitor** (4x/day) sees a READY row with a col Q http link → flips it to **ACTIVE** automatically

## Statuses

- **PENDING** · newly discovered, waiting for research
- **SCORED** · Tier 1, ready for your review
- **APPROVED** · you greenlit it
- **READY** · listing copy done, ready to export
- **ACTIVE** · live on eBay, fully monitored and protected
- **WATCH** · Tier 2, promising, auto re-scored later
- **PAUSED_DEMAND** · Tier 3, not viable right now
- **PAUSED_OOS** · hidden because Costco is out of stock
- **PAUSED_MARGIN** · margin problem, on hold
- **AUDIT_REVIEW** · flagged for a keep or delete call
- **ENDED** · ended on eBay

## What Runs Automatically

All times Central. All hands free.

**Every day**
- 8:50 am · Savings scan (Costco Member-Only Savings)
- 9:00 am · Research (scores PENDING)
- 9:30 am · Active monitor (stock and price check)
- 9:45 am · Sale Radar (top 10 tracked items on sale)
- 10:00 am · Daily run (APPROVED→READY, copy, OOS recheck)
- 10:10 am · eBay sync (units sold, price mirror, break-even reprice)
- 11:00 am · Graveyard sweep (re-checks removed items, auto re-adds revives as PENDING)
- 1:00 pm · Active monitor
- 2:10 pm · eBay sync
- 6:00 pm · Active monitor
- 6:10 pm · eBay sync
- 9:00 pm · Active monitor
- 10:10 pm · eBay sync

**On other timers**
- Every 10 min · sale-end repricing check
- Every 2 days · audit (removes junk, flags borderline)
- Sunday · rotation (weekly underperformer digest)

## The Bot Buttons

Home opens to the Dashboard, the hub for everything.

**📊 Dashboard** · from here you can reach every screen:
- 🟢 **Active Listings** · your live listings, sortable by net or views, with reprice and end controls
- 🔥 **Sales** · what's on sale, with a scrape sale dates action
- 📦 **Stock** · stock status across your items
- 🚨 **Alerts** · sale expiring, stale items, back in stock, plus the audit queue
- ✅ **Review Items** · the SCORED queue
- 📤 **Export CSV** · generate the Seller Hub file
- 💰 **Spot Prices** · live gold and silver spot
- 📈 **Category ROI** · returns by category
- ⚙️ **Run a Mode** · trigger any automation manually
- 🔄 **Refresh** · reload the dashboard

**Review Items** · each card offers:
- ✅ Approve · ⏸️ Pause · 🔍 Send to Audit

**Audit queue** (inside Alerts) · each card offers:
- ✅ Keep · 🗑️ Archive · ⏭️ Skip

## The Automations That Protect You

- **Break-even auto-reprice** · every live listing is held at its break-even floor, so a price drop can never make you lose money
- **Out-of-stock protection** · hides the listing when Costco runs out, restores it when stock returns
- **Sale-end repricing** · reprices on schedule when a Costco sale ends
- **⚡ Competitive pricing** · price $0.01 under the cheapest live competitor
- **Sale Radar** · a daily digest of your tracked items that are actually on sale

## The Automations That Build For You

- **Discovery** · finds new Costco products and files them as PENDING
- **Research and scoring** · grades each product on margin, demand, competition, availability, and fulfillment, then assigns a tier (1, 2, or 3)
- **Listing copy** · writes the SEO title, bullets, description, meta, and keywords
- **Branded photos** · applies your watermark template to the main image
- **CSV export** · one file, Seller Hub ready

## Your Daily Flow (about 10 minutes)

1. Open the bot → **Review Items**
2. Approve the SCORED items you want
3. The daily run flips them to READY with full listing copy
4. Run **Export CSV**, upload the file to eBay Seller Hub
5. Paste each live URL into column Q
6. Done · the monitor takes over from there
