# Workflow: Research
**Layer:** Workflow (Layer 1 — Instructions)
**Status:** Phase 2 — ready to implement
**Agent reads this:** Yes — SOP for 3-pass product research and Tier assignment

---

## Objective

Analyze a new Costco product URL through a 3-pass research process and assign it to Tier 1 (list now), Tier 2 (watch/recheck), or Tier 3 (skip with reason logged). Output goes to the Google Sheet and a daily Tier 1 digest email.

---

## When This Runs

- Manually: when you paste a new Costco URL into the sheet and want a research opinion
- Automatically (Phase 3): as a scheduled weekly research sweep on Tier 2 watchlist items
- Triggered: when monitor.md detects a "Limited / While Supplies Last" status — immediate research run

---

## Required Inputs

- Costco product URL
- Product title (from sheet col A or scraped from Costco page)
- Category (from sheet col C)
- Costco cost (from sheet col E)
- eBay target sell price (from sheet col F, or estimate based on comp research)

---

## Tools Used

1. `tools/costco_scraper.py` — `scrape_costco(url)` for current price, stock, title
2. `tools/ebay_comps.py` — `get_ebay_comps(title, category)` for fee rate and comp data
3. `skills/research_gold.py` OR `skills/research_outdoor.py` — category-specific 3-pass research
4. `tools/tier_scorer.py` — `score_product()` and `assign_tier()` for final Tier decision
5. `tools/sheet_writer.py` — write Tier, scores, and reasoning to sheet
6. `tools/alert_sender.py` — send Tier 1 digest email

---

## The 3-Pass Research System

### Pass 1 — Broad ("Who buys this, and what do they search for?")

**Input:** product title + category
**Goal:** Understand the buyer, the search intent, and what terms drive discovery

Research to conduct:
- What category of buyer is this for? (gift giver, collector, investor, home decorator, etc.)
- What search terms do they use on eBay? (not marketing language — actual search queries)
- What are the top 3 competing product types on eBay for this niche?
- Any Reddit signals? (r/flipping, r/gold, r/WallStreetSilver, r/patio, r/frugal)
- Google Trends trajectory for the top search term (rising / flat / declining)?

**Output:** list of 5–8 eBay search terms, buyer persona summary, trend direction

---

### Pass 2 — Narrow ("How does this actually sell on eBay?")

**Input:** search terms from Pass 1 + costco_cost
**Goal:** Quantify real demand, competition, and margin potential

Research to conduct:
- Search eBay Sold Listings for the top 3 search terms from Pass 1
- Pull: sold count last 90 days, average sold price, active listing count
- Estimate fee rate from categories.yaml for this category
- Calculate margin: (avg_sold_price - costco_cost - (avg_sold_price * fee_rate)) / avg_sold_price
- Competition density: how many active eBay sellers for this exact product?

**Score each dimension 0–10:**
- margin_potential (30% weight): margin > 25% = 10, > 15% = 7, > 10% = 4, < 10% = 1
- demand_signals (25% weight): sold_90d > 50 = 10, > 20 = 7, > 5 = 4, < 5 = 1
- competition_density (20% weight): < 5 active = 10, < 15 = 7, < 30 = 4, > 30 = 1
- costco_availability (15% weight): In Stock = 10, Limited = 6, Seasonal = 4, OOS = 0
- fulfillment_risk (10% weight): lightweight/small = 10, medium = 6, heavy/fragile = 2

**Output:** numeric scores per dimension, estimated margin %, comp count

---

### Pass 3 — Multi-Lens Business Analysis ("Given the numbers, what do we do?")

**Input:** scores from Pass 2 + category + current month (for seasonal modifier)
**Goal:** Four different business minds debate the product. You break the tie.

Apply seasonal modifier first:
- Check categories.yaml seasonal_peak_months for this category
- If current month is in peak season: add +1.0 to the raw weighted score
- If current month is 1 month before peak: add +0.5

**Conservative Analyst (30% of final score)**
Priority weights: margin_potential=0.40, fulfillment_risk=0.30, costco_availability=0.20, competition_density=0.10
Question: "What's my downside? Can I exit fast if this goes wrong?"
Write 1–2 sentences of reasoning from this lens.

**Growth Operator (30% of final score)**
Priority weights: demand_signals=0.40, margin_potential=0.35, competition_density=0.15, fulfillment_risk=0.10
Question: "What's the upside if I'm right about this trend?"
Write 1–2 sentences of reasoning from this lens.

**Brand Builder (20% of final score)**
Priority weights: competition_density=0.35, costco_availability=0.30, demand_signals=0.25, fulfillment_risk=0.10
Question: "Does this product build category authority on my site?"
Write 1–2 sentences of reasoning from this lens.

**Volume Flipper (20% of final score)**
Priority weights: demand_signals=0.40, competition_density=0.35, fulfillment_risk=0.15, margin_potential=0.10
Question: "Can I turn 10 units fast with minimal handling effort?"
Write 1–2 sentences of reasoning from this lens.

**Final weighted score** = (conservative * 0.30) + (growth * 0.30) + (brand * 0.20) + (volume * 0.20) + seasonal_modifier

---

## Tier Assignment Rules

| Score | Tier | Action |
|-------|------|--------|
| >= 7.0 | **Tier 1** | List now — send to Tier 1 digest email, flag for approval |
| 4.0–6.9 | **Tier 2** | Watch — add to data/tier2_watchlist.json, recheck in 7 days |
| < 4.0 | **Tier 3** | Skip — log to data/tier3_skipped.json with reason |

**Tier 1 cap:** Maximum 3 Tier 1 products per research run. If more than 3 qualify, rank by score and demote the rest to Tier 2.

---

## Output to Sheet

Write these fields when research completes:

| Column | Field | Value |
|--------|-------|-------|
| S | sold_90d | From Pass 2 research |
| T | avg_price | Average eBay sold price from Pass 2 |
| U | comp_count | Active eBay listing count from Pass 2 |
| V | demand_score | Weighted score (0–10) from Pass 3 |
| AJ | notes | One-line summary: "Tier X — [top reason]" |

---

## Output to Data Files

**data/tier2_watchlist.json** (append for Tier 2):
```json
{
  "title": "Product Name",
  "costco_url": "https://...",
  "category": "Jewelry",
  "scored_date": "2026-05-07",
  "recheck_date": "2026-05-14",
  "score": 5.8,
  "reason": "Demand moderate, margin solid, watching competition"
}
```

**data/tier3_skipped.json** (append for Tier 3):
```json
{
  "title": "Product Name",
  "costco_url": "https://...",
  "category": "Outdoor Furniture",
  "scored_date": "2026-05-07",
  "score": 2.1,
  "reason": "Competition too high (47 active sellers), margin below 8%"
}
```

---

## Tier 1 Digest Email

Send one email per research run summarizing all Tier 1 products found:

Subject: `"[Tier 1] {count} product(s) ready for your approval — {date}"`

Body:
```
3 products scored Tier 1 from today's research run.
Review and set AG = APPROVED in your sheet to publish.

1. [Product Title] — Score: 8.2
   Conservative: "Strong 24% margin, in stock, lightweight"
   Growth: "Sold 67 units last 90 days on eBay, trending up"
   Brand: "Gold category anchor product for jewelry site"
   Volume: "Only 8 active eBay listings — fast flip potential"
   Recommendation: List now. Prioritize photo quality.

   Sheet row: 7
   Costco URL: https://...

[Sheet link]
```

---

## Error Handling

- **Pass 1 fails (search API blocked):** Self-correct once with different search terms. If fails again, send diagnostic alert with full error and stop.
- **Pass 2 returns no sold listings:** Score demand_signals = 1 (not zero — product may be new). Note in reasoning.
- **eBay API not yet connected:** Use Terapeak manually for comp data and enter into sheet columns S, T, U before running Pass 3.

---

## Self-Improvement Notes

*Update this section as you refine the research process.*

- Gold products: always cross-reference spot gold price from a free API (e.g., metals-api.com free tier) against Costco price. If Costco is within 5% of spot, margin will be thin.
- Outdoor furniture: check shipping weight in product description. Items over 50 lbs cut into margin significantly.
- The 4-lens debate is most valuable when lens scores diverge widely (e.g., Growth = 9, Conservative = 4). That variance means real debate is needed — surface it clearly in the digest email.
