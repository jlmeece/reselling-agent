"""
Skill: research_outdoor
3-pass research for Costco outdoor furniture and patio products.

Extends: C:\\Users\\jorda\\.claude\\skills\\base_research.ResearchBase
Project: reselling-agent

What this skill adds over the base:
  - Outdoor-specific Pass 1 signals (seasonal, shipping weight, HOA market)
  - Outdoor-specific Pass 2 notes (pickup-only comp adjustment, set vs single pricing)
  - Fulfillment risk defaults to "high" (large, heavy items eat into margin fast)

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


class OutdoorResearch(ResearchBase):
    CATEGORY_NAME    = "Outdoor Furniture"
    FULFILLMENT_RISK = "high"   # heavy items, freight shipping, damage risk

    PASS1_SIGNALS = [
        "eBay completed listings — search '{title}', '{brand} patio set', 'outdoor furniture'",
        "Reddit: r/patio, r/malelivingspace, r/frugal, r/deals for demand signals",
        "Google Trends: '{brand} patio set', 'Costco outdoor furniture {year}'",
        "Seasonal check: March-June is peak — seasonal_modifier boosts score in these months",
        "HOA-friendly materials? (low-maintenance resin wicker = broader buyer pool)",
        "Check Costco listing for item dimensions and weight — heavy items cut resale margin",
        "Local pickup vs shipped: if most eBay comps are pickup-only, your shipped listing has less competition",
    ]

    PASS2_NOTES = """
For outdoor furniture, also factor:
- Shipping weight: items over 50 lbs cost $80-200 to ship — deduct from net margin calculation
- Pickup-only eBay listings: if most comps are "local pickup only," your shipped listing
  actually has LESS competition from serious buyers. Adjust competition_density score upward.
- Sets vs singles: a 7-piece patio set is harder to ship but commands higher price premium.
  Check if Costco sells the set AND individual pieces — if both, comps for the full set are cleaner.
- Assembly required: adds buyer hesitation but reduces competition from casual flippers
- Fire pit sets: especially strong demand Sept-Nov (fall outdoor entertaining)
- POLYWOOD and Trex specifically: commands strong resale premiums (eco/weather-resistant brand appeal)
"""

    def run_pass1(self, product_title, category, costco_cost):
        result = super().run_pass1(product_title, category, costco_cost)
        result["shipping_weight_check"] = (
            "Find product weight in Costco listing or manufacturer specs. "
            "Over 75 lbs = high fulfillment risk. Over 150 lbs = freight only."
        )
        result["seasonal_context"] = (
            "Current month determines seasonal modifier. "
            "Mar-Jun = peak (+1.0 score). Feb or Jul = approaching/leaving peak (+0.5)."
        )
        return result

    def run_pass2(self, search_terms, costco_cost, fee_rate):
        result = super().run_pass2(search_terms, costco_cost, fee_rate)
        result["outdoor_notes"] = self.PASS2_NOTES
        result["pickup_only_check"] = (
            "Run eBay search with 'sold listings' filter. "
            "Count how many results show 'local pickup' vs shipped. "
            "If >60% are pickup-only, competition for shipped listings is lower than raw count suggests."
        )
        return result


# ── Module-level functions for backwards compatibility ────────────────────────
# researcher.py calls outdoor_pass3() directly — keep this working.

_outdoor = OutdoorResearch()

def run_pass3(dimension_scores, category_config, reasoning=None):
    """Backwards-compatible module-level entry point."""
    return _outdoor.run_pass3(dimension_scores, category_config, reasoning)

# Alias used in researcher.py
outdoor_pass3 = run_pass3
