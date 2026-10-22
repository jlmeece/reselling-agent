# 🔬 The Reselling Engine · Technical Appendix

The engineering underneath the brochure. For when someone asks "so how does it actually work."

## Architecture at a Glance

- 52 tools · 6 agents · 8 scoring skills · 67 test files
- ~30,000 lines of Python · 309 commits · first commit May 2026
- Stack: Python · Playwright (real Chrome automation) · Google Sheets API · eBay Trading API · Claude API · Telegram Bot API

## The Research Methodology (3-pass)

1. **Pass 1 · Broad** · who buys this, what do they search, buyer personas, trend direction
2. **Pass 2 · Narrow** · real eBay sold data, margin math, numeric dimension scores
3. **Pass 3 · Score** · four-lens business analysis → tier assignment + reasoning

Category-specific research skills tune each pass: appliances, watches, outdoor, pharmacy, jewelry, precious metals.

## The Scoring Engine

**5 dimensions** · each graded 0 to 10:
- margin potential · weight 0.30
- demand signals · weight 0.25
- competition density · weight 0.20
- Costco availability · weight 0.15
- fulfillment risk · weight 0.10

**4 business lenses** · each re-weights the dimensions by business priority:

- conservative analyst · margin 0.40, fulfillment 0.30, availability 0.20, competition 0.10
- growth operator · demand 0.40, margin 0.35, competition 0.15, fulfillment 0.10
- brand builder · competition 0.35, availability 0.30, demand 0.25, fulfillment 0.10
- volume flipper · demand 0.40, margin 0.30, competition 0.15, fulfillment 0.15

**Final combination** · volume flipper 0.40, conservative analyst 0.25, growth operator 0.25, brand builder 0.10

**Tier thresholds** · Tier 1 ≥ 5.0 (list now), Tier 2 ≥ 3.0 (watch), Tier 3 < 3.0 (shelve)

**Seasonal modifier** · +1.0 in a category's peak month, +0.5 the month before

## Dimension Scoring Curves

**margin potential** · >15% = 10 · >10% = 8 · >5% = 6 · >2% = 4 · >0% = 2 · else 1

**demand signals** (90-day sold) · >30 = 10 · >15 = 8 · >8 = 6 · >5 = 5 · >1 = 3 · else 1

**competition density** (active listings) · <10 = 10 · <25 = 8 · <60 = 6 · else 4

**Costco availability** · In Stock = 10 · Limited = 6 · Seasonal = 4 · Unknown = 3 · Check Failed = 1 · OOS = 0

**fulfillment risk** · low = 10 · medium = 6 · high = 2

## Net-Profit Scoring

Absolute dollars per unit, not margin percent. Roughly one point per doubling:

$300+ = 10 · $150+ = 9 · $75+ = 8 · $40+ = 7 · $20+ = 6 · $10+ = 5 · $5+ = 4 · $2+ = 3 · $1+ = 2 · else 1

## Monthly-Profit Overrides

Real money can't be shelved by a low margin percent. High-ticket items clear these regardless:

- Monthly profit ≥ $40 → force to SCORED (review it)
- Monthly profit ≥ $10 → at least WATCH (never paused)

## Sell-Through Rate

sold_90d ÷ (active_count × 3). >1.0 = 10 · >0.5 = 8 · >0.2 = 5 · >0.1 = 3 · else 1

## eBay Comp Research

Scrapes real eBay data through a live Chrome session (avoids bot detection):

**Sold listings** · completed + sold, new condition, buy it now, US only → real sold counts and average sold price

**Active listings** · new condition, buy it now, US only, free shipping, sorted lowest price first → the live competitor field

**Signal hygiene** · strips noise words (Kirkland, Signature, marketing filler), drops junk listings (parts, broken, OEM, salvage), keeps model numbers and size variants, caps queries at 8 tokens

## The Fee Model

Real drop-shipped profit per unit, not naive margin:

- Net = eBay price − Costco cost − eBay final value fee − fees − Costco sales tax
- eBay final value fee = price × category fee rate × 1.08 (buyer tax) + $0.30 per order
- Costco sales tax = 8.25%
- Category fee rates vary (Small Appliances 13.25%, and so on)

## Portfolio Ranking (MPT / Sharpe)

Every product is treated like an asset in a portfolio and ranked by risk-adjusted return. The sheet carries a Sharpe score and a portfolio rank, so effort flows to the best return per unit of risk, not just the biggest dollar number.

## eBay Sync

- Trading API pulls units sold back into the sheet
- The sheet's eBay price column mirrors the live listing price
- Detects margin breaches (net below zero)
- Auto-reprices hard breaches to break-even, so no live listing ever sells at a loss
- Flags listings that are live on eBay but missing from the sheet

## Automation Modes (19)

active · daily · research · discovery · rotation · audit · graveyard-sweep · ebay_sync · apply_prices · sale-digest · savings · apply_scheduled · sale-refresh · backfill_lowest · health · discovery-stats · export · recheck · refresh-notes

Each is a standalone mode driven by one scheduler, running on the Windows Task Scheduler and controlled from the Telegram bot.
