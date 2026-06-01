"""
Shared Skill: base_scoring
Universal multi-lens Tier 1/2/3 scoring engine.

Lives in: C:\\Users\\jorda\\.claude\\skills\\base_scoring.py
Used by:  ANY project that assigns product/deal tiers based on multi-dimension scoring.

This file is bundled in the project's skills/ directory for CI compatibility.
The authoritative version lives at C:\\Users\\jorda\\.claude\\skills\\base_scoring.py.
When improving scoring logic, edit the global version — then sync this copy.

Scoring model:
  - 5 dimensions, each scored 0-10 by Claude or deterministic rules
  - 4 business lenses, each applying different dimension weights
  - Lenses combined into a final weighted score
  - Seasonal modifier applied last (+0.5 to +1.0 during peak months)
  - Tier 1 >= 7.0 | Tier 2 >= 4.0 | Tier 3 < 4.0
"""

from datetime import datetime


# ── Dimension weights (how much each factor contributes to the overall score) ──

SCORING_DIMENSIONS = {
    "margin_potential":    0.30,   # (sell - cost - fees) / sell
    "demand_signals":      0.25,   # eBay sold count, search trends
    "competition_density": 0.20,   # fewer active listings = higher score
    "costco_availability": 0.15,   # In Stock > Limited > Seasonal > OOS
    "fulfillment_risk":    0.10,   # weight, fragility, shipping complexity
}


# ── Lens definitions (each lens reweights the dimensions by business priority) ──

LENS_WEIGHTS = {
    "conservative_analyst": {
        # Risk-first: margin and availability before everything
        "margin_potential":    0.40,
        "fulfillment_risk":    0.30,
        "costco_availability": 0.20,
        "competition_density": 0.10,
    },
    "growth_operator": {
        # Upside-first: demand trajectory and margin potential
        "demand_signals":      0.40,
        "margin_potential":    0.35,
        "competition_density": 0.15,
        "fulfillment_risk":    0.10,
    },
    "brand_builder": {
        # Authority-first: low competition + availability builds category presence
        "competition_density": 0.35,
        "costco_availability": 0.30,
        "demand_signals":      0.25,
        "fulfillment_risk":    0.10,
    },
    "volume_flipper": {
        # Speed-first: high demand, low competition, easy to ship
        "demand_signals":      0.40,
        "competition_density": 0.35,
        "fulfillment_risk":    0.15,
        "margin_potential":    0.10,
    },
}


# ── How lenses combine into the final score ──

FINAL_LENS_WEIGHTS = {
    "conservative_analyst": 0.25,
    "growth_operator":      0.25,
    "brand_builder":        0.10,
    "volume_flipper":       0.40,
}


# ── Tier thresholds ──

TIER_RULES = {
    "tier1": 6.0,
    "tier2": 3.0,
}

MAX_TIER1_PER_RUN = 3


# ── Scoring functions ──────────────────────────────────────────────────────────

def score_dimension(dimension, value):
    """
    Convert a raw metric value into a 0-10 score for a given dimension.
    These thresholds are business-neutral — they work for any reselling context.
    Override in project-specific code if your category has different economics.
    """
    if dimension == "margin_potential":
        if value is None:
            return 0
        margin = value
        if margin > 0.15:  return 10
        if margin > 0.10:  return 8
        if margin > 0.05:  return 6
        if margin > 0.02:  return 4
        if margin > 0:     return 2
        return 1

    elif dimension == "demand_signals":
        if value is None:
            return 1   # unknown ≠ zero — product may simply be new
        if value > 30:   return 10
        elif value > 15: return 8
        elif value > 5:  return 5
        elif value > 1:  return 3
        else:            return 1

    elif dimension == "competition_density":
        if value is None:
            return 5   # unknown → assume moderate
        if value < 10:   return 10
        elif value < 25: return 7
        elif value < 60: return 4
        else:            return 1

    elif dimension == "costco_availability":
        return {
            "In Stock":      10,
            "Limited":        6,
            "Seasonal":       4,
            "OUT OF STOCK":   0,
            "Unknown":        3,
            "CHECK FAILED":   1,
        }.get(value, 3)

    elif dimension == "fulfillment_risk":
        return {
            "low":    10,
            "medium":  6,
            "high":    2,
        }.get(value, 6)

    return 5   # unknown dimension → neutral score


def score_sell_through(sold_90d, active_count):
    if not sold_90d or not active_count:
        return 3  # unknown → neutral
    rate = sold_90d / max(active_count * 3, 1)
    if rate > 1.0:   return 10
    elif rate > 0.5: return 8
    elif rate > 0.2: return 5
    elif rate > 0.1: return 3
    else:            return 1


def apply_seasonal_modifier(base_score, category_config):
    """
    Adds +1.0 (in peak month) or +0.5 (month before peak) to the base score.
    peak_months is a list of month numbers, e.g. [3, 4, 5, 6] for spring.
    """
    peak_months = category_config.get("seasonal_peak_months", [])
    current_month = datetime.now().month

    if current_month in peak_months:
        return base_score + 1.0
    elif (current_month % 12 + 1) in peak_months:   # month before peak starts
        return base_score + 0.5
    return base_score


def assign_tier(weighted_score):
    """Convert a final weighted score (0-10) to Tier 1, 2, or 3."""
    if weighted_score >= TIER_RULES["tier1"]:
        return 1
    elif weighted_score >= TIER_RULES["tier2"]:
        return 2
    else:
        return 3


def calculate_lens_score(lens_name, dimension_scores):
    """Apply one lens's dimension weights → single lens score (0-10)."""
    weights = LENS_WEIGHTS[lens_name]
    total = sum(
        dimension_scores.get(dim, 5) * weight
        for dim, weight in weights.items()
    )
    return round(total, 2)


def calculate_final_score(lens_scores, category_config):
    """
    Combine all lens scores with seasonal modifier → final score (0-10+).
    """
    base = sum(
        lens_scores[lens] * weight
        for lens, weight in FINAL_LENS_WEIGHTS.items()
    )
    return round(apply_seasonal_modifier(base, category_config), 2)
