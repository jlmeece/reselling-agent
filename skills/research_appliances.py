"""
Skill: research_appliances
3-pass research for Costco small kitchen appliances (Vitamix, KitchenAid, Instant Pot, etc.)

Extends: C:\\Users\\jorda\\.claude\\skills\\base_research.ResearchBase
Project: reselling-agent

What this skill adds over the base:
  - Model-number-first eBay search (like watches — cleanest comps)
  - Costco bundle awareness: Costco frequently includes extra attachments not in retail box
  - Brand-tier scoring: Vitamix/KitchenAid (strong resale) vs Ninja/Instant Pot (commodity)
  - Fulfillment risk = "medium" — bulky but well under 50 lbs, standard residential shipping

Pass 3 (multi-lens scoring) and research() are inherited from ResearchBase — no duplication.
To change scoring math, edit: C:\\Users\\jorda\\.claude\\skills\\base_scoring.py
"""

import sys
import os

# Try global skills dir (Windows local dev), fall back to bundled copy in skills/ (CI/Ubuntu)
_GLOBAL_SKILLS = r"C:\Users\jorda\.claude\skills"
_LOCAL_SKILLS  = os.path.dirname(os.path.abspath(__file__))
for _p in [_GLOBAL_SKILLS, _LOCAL_SKILLS]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from base_research import ResearchBase
from base_scoring import LENS_WEIGHTS


class AppliancesResearch(ResearchBase):
    CATEGORY_NAME    = "Small Appliances"
    FULFILLMENT_RISK = "medium"   # bulky boxes, but well under 50 lbs

    PASS1_SIGNALS = [
        "eBay completed listings — search exact model number first, then '{brand} {type} costco'",
        "Identify if Costco bundle includes extra accessories not in standard retail box",
        "Reddit: r/Costco, r/BuyItForLife, r/frugal for product-specific demand signals",
        "Google Trends: '{brand} {model}', 'costco {brand} deal'",
        "eBay WatchCount: watchers on top 3 active listings?",
        "Check if item is Costco-exclusive bundle SKU — reduces direct eBay competition",
        "Brand tier: Vitamix/KitchenAid = premium resale. Ninja/Instant Pot = commodity (price-sensitive).",
        "Costco pricing vs MSRP — Costco typically 15-25% below retail for premium brands",
    ]

    PASS2_NOTES = """
For small appliances, also factor:
- Bundle delta: Costco often bundles extra bowls, pitcher sizes, or attachments. If the eBay
  comp is for the base model only, your Costco bundle commands a meaningful premium. Quantify it.
- Brand resale tiers:
    Tier A (strong resale, premium): Vitamix, KitchenAid, Breville, De'Longhi
    Tier B (decent resale, mid): Cuisinart, Ninja, Instant Pot, Philips
    Tier C (commodity, skip): generic/house brands
- Shipping: most small appliances ship standard residential (UPS/FedEx). Under 30 lbs = no
  freight risk. Check product weight on Costco listing.
- Condition sensitivity: opened-box appliances drop 20-35% in resale value — always list as NEW.
- Return rate risk: high-wattage appliances (blenders, mixers) have higher buyer remorse.
  Check eBay sold listings for condition patterns ("tested", "no returns") to gauge category risk.
- Warranty note: Costco adds 2-year Concierge warranty — this is a legitimate selling point on eBay.
"""

    def run_pass1(self, product_title, category, costco_cost):
        result = super().run_pass1(product_title, category, costco_cost)
        result["bundle_check"] = (
            "Identify all items included in the Costco box vs standard retail version. "
            "Extra accessories = bundle premium. Quantify vs eBay base-model comps."
        )
        result["brand_tier_check"] = (
            "Classify brand: Vitamix/KitchenAid/Breville = Tier A (premium resale). "
            "Ninja/Cuisinart/Instant Pot = Tier B (commodity pricing). Adjust demand and margin scores accordingly."
        )
        return result

    def run_pass2(self, search_terms, costco_cost, fee_rate):
        result = super().run_pass2(search_terms, costco_cost, fee_rate)
        result["appliance_notes"] = self.PASS2_NOTES
        result["suggested_searches"] = [
            f"{t} sold ebay" for t in search_terms
        ] + [
            "vitamix costco bundle sold ebay",
            "kitchenaid stand mixer costco sold ebay",
        ]
        return result


# ── Module-level entry points ─────────────────────────────────────────────────

_appliances = AppliancesResearch()

def run_pass3(dimension_scores, category_config, reasoning=None):
    return _appliances.run_pass3(dimension_scores, category_config, reasoning)

appliances_pass3 = run_pass3
