"""
Skill: research_pharmacy
3-pass research for Costco vitamins, supplements, and OTC health products.

Extends: C:\\Users\\jorda\\.claude\\skills\\base_research.ResearchBase
Project: reselling-agent

What this skill adds over the base:
  - Bulk-pack / unit-price eBay comp logic (Costco sells 500-count bottles; eBay buyers
    often buy 180-count — compare price-per-unit, not total price)
  - Kirkland Signature brand premium awareness
  - Expiry date risk factor (supplements lose resale value near expiry — score down)
  - Fulfillment risk = "low" — lightweight bottles, non-fragile, easy to ship

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


class PharmacyResearch(ResearchBase):
    CATEGORY_NAME    = "Pharmacy"
    FULFILLMENT_RISK = "low"   # lightweight bottles, non-fragile, standard poly mailer

    PASS1_SIGNALS = [
        "eBay completed listings — search '{brand} {product} {count}ct' for exact pack size comps",
        "Calculate Costco price-per-unit vs eBay price-per-unit — buyers compare this directly",
        "Reddit: r/Costco, r/Supplements, r/frugal for demand signals and reviews",
        "Google Trends: '{brand} {product}', 'costco {product} worth it'",
        "Is this Kirkland Signature? KS brand commands a loyalty premium on eBay",
        "Check expiry date on Costco listing — supplements within 12 months of expiry = skip",
        "eBay active competing listings: how many sellers? Supplements are competitive but high-velocity",
        "Name-brand equivalent: if Costco sells Fish Oil 1000mg, compare to Nordic Naturals eBay price",
    ]

    PASS2_NOTES = """
For vitamins and supplements, also factor:
- Unit count vs eBay comp: Costco sells 500ct fish oil; most eBay listings are 90-180ct.
  Always normalize to price-per-capsule before scoring margin_potential.
  Example: Costco 500ct @ $20 = $0.04/cap. eBay 180ct @ $18 = $0.10/cap. Strong margin.
- Kirkland Signature premium: KS supplements are manufactured by the same companies as
  name brands (Puritan's Pride, etc.). Buyers know this — KS commands loyalty, not discount.
- Expiry risk: note approximate manufacturing date from lot number if visible.
  Supplements with < 12 months shelf life at time of listing have higher return risk.
- eBay supplement category restrictions: most standard vitamins/minerals are unrestricted.
  Avoid: weight loss claims, testosterone boosters, anything with "proprietary blend" + exotic ingredients.
  These can trigger eBay listing policy flags.
- Fulfillment: poly mailer or small box. Most supplement bottles are < 2 lbs.
  High margin-to-weight ratio = excellent fulfillment economics.
- Velocity: supplement buyers reorder frequently. A strong listing builds repeat traffic.
"""

    def run_pass1(self, product_title, category, costco_cost):
        result = super().run_pass1(product_title, category, costco_cost)
        result["unit_price_check"] = (
            "Find Costco count size (e.g. 500ct). Find most common eBay listing size. "
            "Calculate price-per-unit for both. Margin is the per-unit spread × Costco count."
        )
        result["policy_check"] = (
            "Confirm product is a standard vitamin/mineral/supplement with no health claims "
            "that would trigger eBay listing restrictions. When in doubt, skip."
        )
        return result

    def run_pass2(self, search_terms, costco_cost, fee_rate):
        result = super().run_pass2(search_terms, costco_cost, fee_rate)
        result["pharmacy_notes"] = self.PASS2_NOTES
        result["suggested_searches"] = [
            f"{t} sold ebay" for t in search_terms
        ] + [
            "kirkland fish oil 500 sold ebay",
            "kirkland vitamins costco sold ebay",
        ]
        return result


# ── Module-level entry points ─────────────────────────────────────────────────

_pharmacy = PharmacyResearch()

def run_pass3(dimension_scores, category_config, reasoning=None):
    return _pharmacy.run_pass3(dimension_scores, category_config, reasoning)

pharmacy_pass3 = run_pass3
