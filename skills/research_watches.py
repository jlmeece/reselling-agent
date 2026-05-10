"""
Skill: research_watches
3-pass research for Costco watches (Citizen, Luminox, Bering, Seiko, etc.)

Extends: C:\\Users\\jorda\\.claude\\skills\\base_research.ResearchBase
Project: reselling-agent

Watches score differently from gold:
  - Brand recognition matters more than spot price
  - Condition and box/papers are major price drivers on eBay
  - Eco-Drive/solar watches have premium resale vs quartz
  - Luxury-adjacent brands (Citizen, Luminox) have loyal buyer bases
  - Seasonal: Father's Day (June) and holiday season (Nov-Dec) are peaks

Fulfillment risk = "low" — watches ship small and insured easily.
"""

import sys
import os

_SHARED_SKILLS = r"C:\Users\jorda\.claude\skills"
if _SHARED_SKILLS not in sys.path:
    sys.path.insert(0, _SHARED_SKILLS)

from base_research import ResearchBase
from base_scoring import LENS_WEIGHTS


class WatchResearch(ResearchBase):
    CATEGORY_NAME    = "Watches"
    FULFILLMENT_RISK = "low"   # small box, easy to insure

    PASS1_SIGNALS = [
        "eBay completed listings — search '{brand} {model}' and '{model number if visible}'",
        "Reddit: r/Watches, r/WatchHorology, r/frugal for brand sentiment",
        "Google Trends: '{brand} watch review', '{model} eBay'",
        "eBay WatchCount: how many watchers on top active listings?",
        "Does it come with original box and papers? Box+papers = significant premium on eBay",
        "Is this an Eco-Drive/solar model? Solar commands resale premium over quartz",
        "Costco watch pricing vs MSRP — Costco typically sells 20-40% below MSRP",
        "Check: is this a Costco exclusive colorway? (reduces direct eBay competition)",
    ]

    PASS2_NOTES = """
For watches, also factor:
- Box and papers: eBay buyers pay 15-30% premium for watches sold with original packaging.
  Costco includes original boxes — this is a competitive advantage worth noting.
- Brand tier: Citizen/Seiko = accessible luxury (strong resale). Luminox = tactical niche
  (loyal but smaller buyer pool). Bering = Nordic design niche (moderate demand).
- Eco-Drive solar vs quartz: solar models hold value better and attract serious watch buyers.
- Reference number: search eBay by the exact model/reference number for cleanest comps.
- Condition risk: watches scratch easily. Factor photo/packaging care into listing quality.
- Father's Day (June) and Q4 holiday season are the two demand peaks for watch resale.
"""

    def run_pass1(self, product_title, category, costco_cost):
        result = super().run_pass1(product_title, category, costco_cost)
        result["brand_check"] = (
            "Identify exact brand and model number from Costco listing. "
            "Search eBay by model number for cleanest sold comps."
        )
        result["packaging_check"] = (
            "Confirm watch ships with original box and papers. "
            "This is standard for Costco — note it as a resale advantage."
        )
        return result

    def run_pass2(self, search_terms, costco_cost, fee_rate):
        result = super().run_pass2(search_terms, costco_cost, fee_rate)
        result["watch_notes"] = self.PASS2_NOTES
        result["suggested_searches"] = [
            f"{t} sold ebay" for t in search_terms
        ] + [
            "citizen eco drive costco sold ebay",
            "luminox watch sold ebay",
        ]
        return result


# ── Module-level entry points ─────────────────────────────────────────────────

_watches = WatchResearch()

def run_pass3(dimension_scores, category_config, reasoning=None):
    return _watches.run_pass3(dimension_scores, category_config, reasoning)

watches_pass3 = run_pass3
