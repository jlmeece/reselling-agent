"""
Skill: research_gold
3-pass research for Costco gold, coins, silver, and jewelry products.

Extends: C:\\Users\\jorda\\.claude\\skills\\base_research.ResearchBase
Project: reselling-agent

What this skill adds over the base:
  - Gold-specific Pass 1 signals (spot price, assay, karat, Reddit gold communities)
  - Gold-specific Pass 2 notes (weight class, authentication premium, spot markup)
  - Fulfillment risk = "low" (gold ships in small boxes, low breakage risk)

Pass 3 (multi-lens scoring) and research() are inherited from ResearchBase — no duplication.
To change scoring math, edit: C:\\Users\\jorda\\.claude\\skills\\base_scoring.py
"""

import sys
import os

_SHARED_SKILLS = r"C:\Users\jorda\.claude\skills"
if _SHARED_SKILLS not in sys.path:
    sys.path.insert(0, _SHARED_SKILLS)

from base_research import ResearchBase
from base_scoring import LENS_WEIGHTS   # kept for backwards compatibility


class GoldResearch(ResearchBase):
    CATEGORY_NAME    = "Precious Metals"
    FULFILLMENT_RISK = "low"   # gold ships small, minimal breakage risk

    PASS1_SIGNALS = [
        "eBay completed listings — search '{title} gold' and '{title} coin'",
        "Reddit: r/WallStreetSilver, r/Gold, r/Costco for real buyer sentiment",
        "Google Trends: 'gold bar costco', 'gold coin investment', '{title}'",
        "eBay WatchCount: how many watchers on top 3 active listings?",
        "Spot gold/silver price vs Costco unit price — within 5% of spot = thin margin",
        "Any recent Costco gold posts going viral on Reddit? (demand spike signal)",
        "Check: does product include COA or assay card? (affects buyer trust + premium)",
    ]

    PASS2_NOTES = """
For gold and precious metals, also factor:
- Spot premium: if Costco is within 3% of spot price, resale margin will be razor thin.
  Check kitco.com or goldprice.org for live spot. Factor this into margin_potential score.
- Authentication: assay card or COA = +0.5 buyer trust premium on eBay vs unverified
- Weight class matters: 1oz coins vs bars command different premiums — run separate eBay searches
- Historical premium: Costco gold typically sells 1-5% over spot on eBay
- Silver: lower absolute margin but higher velocity — factor sell-through rate over dollar margin
- Jewelry (non-bullion): karat, brand (Pandora, Tiffany-adjacent), and style drive price variance
"""

    def run_pass1(self, product_title, category, costco_cost):
        result = super().run_pass1(product_title, category, costco_cost)
        result["gold_spot_check"] = (
            "Compare Costco price to live spot at kitco.com. "
            "If Costco price / spot price ratio > 1.05, margin is viable."
        )
        result["authentication_check"] = (
            "Does product listing mention assay card, COA, or mint certification? "
            "If yes, +premium on eBay. If no, note risk."
        )
        return result

    def run_pass2(self, search_terms, costco_cost, fee_rate):
        result = super().run_pass2(search_terms, costco_cost, fee_rate)
        result["spot_premium_note"] = self.PASS2_NOTES
        result["suggested_searches"] = [
            f"{t} ebay sold" for t in search_terms
        ] + [
            "1 oz gold bar costco sold ebay",
            "costco gold coin completed listings",
        ]
        return result


# ── Module-level functions for backwards compatibility ────────────────────────
# researcher.py calls gold_pass3() directly — keep this working.

_gold = GoldResearch()

def run_pass3(dimension_scores, category_config, reasoning=None):
    """Backwards-compatible module-level entry point."""
    return _gold.run_pass3(dimension_scores, category_config, reasoning)

# Alias used in researcher.py
gold_pass3 = run_pass3
