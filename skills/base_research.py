"""
Shared Skill: base_research
Foundation class for all 3-pass product research skills.

Lives in: C:\\Users\\jorda\\.claude\\skills\\base_research.py
Used by:  Any project that follows the 3-pass research methodology.

This file is bundled in the project's skills/ directory for CI compatibility.
The authoritative version lives at C:\\Users\\jorda\\.claude\\skills\\base_research.py.
When improving the research framework, edit the global version — then sync this copy.

The 3-pass framework:
  Pass 1 — Broad:   Who buys this? What do they search for? Buyer personas.
  Pass 2 — Narrow:  Real eBay sold data. Margin math. Numeric scores.
  Pass 3 — Score:   4-lens business analysis → Tier assignment + reasoning.

Pass 3 is FULLY IMPLEMENTED here and never needs to change between categories.
Pass 1 and Pass 2 return scaffold data — subclasses override with category signals.
The researcher agent fills Pass 1+2 data via Claude API calls and eBay scraping.
"""

import sys
import os

# Add this file's directory to path so base_scoring is findable regardless of where
# this module is imported from.
_SHARED = os.path.dirname(os.path.abspath(__file__))
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from base_scoring import (
    calculate_lens_score,
    calculate_final_score,
    assign_tier,
    LENS_WEIGHTS,
)


class ResearchBase:
    """
    Base class for all category research skills.

    Subclasses must define:
        CATEGORY_NAME   (str)  — e.g. "Jewelry", "Outdoor Furniture"
        PASS1_SIGNALS   (list) — category-specific signals to guide Claude's Pass 1 research
        PASS2_NOTES     (str)  — category-specific notes for eBay comp analysis
        FULFILLMENT_RISK (str) — "low" | "medium" | "high" default for this category

    Subclasses may override:
        run_pass1()  — add category-specific return fields
        run_pass2()  — add category-specific dimension adjustments
        run_pass3()  — rarely needed; the base implementation is complete
    """

    CATEGORY_NAME    = "Generic"
    PASS1_SIGNALS    = [
        "eBay completed listings for this product",
        "Reddit communities for this category",
        "Google Trends for product name and category",
        "eBay WatchCount: watchers on top active listings",
    ]
    PASS2_NOTES      = "Standard product research — no category-specific adjustments."
    FULFILLMENT_RISK = "medium"


    def run_pass1(self, product_title, category, costco_cost):
        """
        Pass 1: Broad research — buyer persona identification and search terms.
        Returns scaffold data. Researcher agent (Claude) populates the real values.

        Returns dict with: search_terms, buyer_personas, trend_direction, notes,
                           plus any category-specific fields added by subclass.
        """
        return {
            "search_terms":    [],
            "buyer_personas":  [],
            "trend_direction": "unknown",
            "notes": (
                f"Run Pass 1 for '{product_title}' ({category}) "
                f"using signals: {self.PASS1_SIGNALS}"
            ),
            "signals": self.PASS1_SIGNALS,
        }


    def run_pass2(self, search_terms, costco_cost, fee_rate):
        """
        Pass 2: Narrow — eBay sold data and margin scoring.
        Returns scaffold data. Researcher agent fills real eBay comp values.

        Returns dict with: sold_90d, avg_sold_price, comp_count, margin_pct,
                           dimension_scores, notes.
        """
        return {
            "sold_90d":        None,
            "avg_sold_price":  None,
            "comp_count":      None,
            "margin_pct":      None,
            "dimension_scores": {
                "margin_potential":    0,
                "demand_signals":      0,
                "competition_density": 0,
                "costco_availability": 0,
                "fulfillment_risk":    0,
            },
            "notes": self.PASS2_NOTES,
        }


    def run_pass3(self, dimension_scores, category_config, reasoning=None):
        """
        Pass 3: Multi-lens business analysis and Tier assignment.
        FULLY IMPLEMENTED — subclasses rarely need to override this.

        dimension_scores: dict populated by the researcher agent from Pass 2
        category_config:  dict from categories.yaml for this category
        reasoning:        dict of lens_name → text, from Claude's analysis
        """
        lens_scores = {
            lens: calculate_lens_score(lens, dimension_scores)
            for lens in LENS_WEIGHTS
        }

        final_score = calculate_final_score(lens_scores, category_config)
        tier        = assign_tier(final_score)

        if reasoning is None:
            reasoning = {k: "" for k in LENS_WEIGHTS}

        recommendations = {
            1: "List now. Review copy and photos before publishing.",
            2: "Watch. Recheck in 7 days or when stock status changes.",
            3: "Skip. Log reason and move on.",
        }

        return {
            "tier":           tier,
            "weighted_score": final_score,
            "lens_scores":    lens_scores,
            "reasoning":      reasoning,
            "recommendation": recommendations[tier],
        }


    def research(self, product_title, category, costco_cost, fee_rate, category_config):
        """
        Full 3-pass pipeline entry point.
        Returns complete tier result ready for writing to sheet and digest email.
        """
        pass1 = self.run_pass1(product_title, category, costco_cost)
        pass2 = self.run_pass2(pass1["search_terms"], costco_cost, fee_rate)
        pass3 = self.run_pass3(pass2["dimension_scores"], category_config)

        return {
            "product_title": product_title,
            "category":      category,
            "pass1":         pass1,
            "pass2":         pass2,
            "result":        pass3,
        }


# ── Convenience: module-level functions for backwards compatibility ───────────
# Projects that call run_pass3() or research() as functions (not via a class)
# can import these directly.

_default = ResearchBase()

def run_pass3(dimension_scores, category_config, reasoning=None):
    return _default.run_pass3(dimension_scores, category_config, reasoning)
