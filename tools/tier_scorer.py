"""
Tool: tier_scorer
Assigns Tier 1 / 2 / 3 to a product based on multi-lens business analysis.

score_product() accepts pre-computed dimension scores and reasoning from the
researcher agent (which uses Claude to reason through Pass 1 + Pass 2), then
applies the deterministic scoring math from skills/scoring.py.

Tier rules:
  Tier 1 (score >= 6.0): SCORED — review and approve to list
  Tier 2 (3.0 <= score < 6.0): WATCH — re-scored weekly
  Tier 3 (score < 3.0): PAUSED_DEMAND — re-eval in 30 days
"""

import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.scoring import (
    calculate_lens_score,
    calculate_final_score,
    assign_tier,
    LENS_WEIGHTS,
)


def score_product(product_data, dimension_scores, reasoning, category_config):
    """
    Applies multi-lens scoring math to Claude-generated dimension scores.

    product_data:      dict — title, category, costco_cost, ebay_price, stock_status
    dimension_scores:  dict — margin_potential, demand_signals, competition_density,
                               costco_availability, fulfillment_risk (each 0-10)
    reasoning:         dict — conservative_analyst, growth_operator, brand_builder,
                               volume_flipper (each a sentence of reasoning text)
    category_config:   dict — from categories.yaml for this category

    Returns: {tier, weighted_score, lens_scores, reasoning, recommendation, recheck_date}
    """
    lens_scores = {
        lens: calculate_lens_score(lens, dimension_scores)
        for lens in LENS_WEIGHTS
    }

    final_score = calculate_final_score(lens_scores, category_config)
    tier = assign_tier(final_score)

    if tier == 1:
        recommendation = "Tier 1 — SCORED. Review notes and approve to list, or set PAUSED_DEMAND to skip."
    elif tier == 2:
        recommendation = "Tier 2 — WATCH. Re-scored weekly; promotes to SCORED if conditions improve."
    else:
        recommendation = "Tier 3 — PAUSED_DEMAND. Re-eval in 30 days."

    return {
        "tier": tier,
        "weighted_score": final_score,
        "lens_scores": lens_scores,
        "dimension_scores": dimension_scores,
        "reasoning": reasoning,
        "recommendation": recommendation,
        "recheck_date": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
    }
