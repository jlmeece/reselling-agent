# 🛒 The Reselling Engine · Full Guide

Costco → eBay drop-shipping, automated end to end. It finds products, scores them, writes the listing, builds your branded photo, then monitors and protects everything that goes live.

*309 commits · 50+ tools · ~30,000 lines of Python · 67 test files · first commit May 2026*

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

## The Intelligence Under the Hood

**Scoring engine** · every product is graded 0 to 10 on five dimensions: margin potential, demand signals, competition density, Costco availability, and fulfillment risk. Four business lenses then weigh those dimensions differently (a conservative analyst, a growth operator, a brand builder, and a volume flipper), and the lens scores combine into one final number with a seasonal adjustment. Tier 1 (5+) is list now, Tier 2 (3 to 4.9) is watch, Tier 3 is shelved.

**Portfolio ranking (MPT / Sharpe)** · every product is treated like an asset in a portfolio and ranked by risk-adjusted return. The sheet carries a Sharpe score and a portfolio rank, so your effort goes to the listings with the best return per unit of risk, not just the biggest dollar number.

**eBay comp research** · scrapes eBay sold and active listings (new condition, buy it now, US only), filters out junk like parts and broken stock, and pulls real sold counts, average sold price, and competitor price range. The ⚡ Competitive price comes from here: it finds the cheapest live competitor and prices $0.01 under.

**eBay sync** · the Trading API pulls units sold back into the sheet, mirrors eBay's live price, detects margin breaches, and flags listings that are live on eBay but missing from the sheet.

**The smart sheet** · the Google Sheet is the living brain. Every row carries status, tier, Costco cost, eBay price, net profit, 90-day velocity, SKU, target price, and portfolio rank. As listings age and sales accumulate, re-scoring pulls fresh eBay sold data, so the sheet gets smarter the longer it runs.

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

- **Break-even auto-reprice** · automatic, every live listing is held at its break-even floor, so a price drop can never make you lose money
- **Out-of-stock protection** · hides the listing when Costco runs out, restores it when stock returns
- **Sale-end repricing** · reprices on schedule when a Costco sale ends
- **⚡ Competitive pricing** · automatic, $0.01 under the cheapest live competitor
- **Sale Radar** · a daily digest of your tracked items that are actually on sale
- **Manual reprice** · from Active Listings, step any price up or down by hand, or end a listing on the spot

## The Automations That Build For You

- **Discovery** · finds new Costco products and files them as PENDING
- **Research and scoring** · grades each product on five dimensions and assigns a tier (1, 2, or 3)
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
