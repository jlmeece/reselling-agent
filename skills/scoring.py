"""
Skill: scoring
Thin re-export of the shared base_scoring library.

On Windows, the canonical engine lives at:
    C:\\Users\\jorda\\.claude\\skills\\base_scoring.py

On VPS/Linux, falls back to the local skills/ directory within the project.
Both resolve to the same base_scoring.py content.
"""

import sys
import os

# Windows path (Jay's dev machine)
_SHARED_SKILLS = r"C:\Users\jorda\.claude\skills"
if _SHARED_SKILLS not in sys.path:
    sys.path.insert(0, _SHARED_SKILLS)

# VPS/Linux fallback — skills/ lives inside the project root
_LOCAL_SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
if _LOCAL_SKILLS not in sys.path:
    sys.path.insert(0, _LOCAL_SKILLS)

# Re-export everything so existing imports continue working unchanged
from base_scoring import (
    SCORING_DIMENSIONS,
    LENS_WEIGHTS,
    FINAL_LENS_WEIGHTS,
    TIER_RULES,
    MAX_TIER1_PER_RUN,
    score_dimension,
    apply_seasonal_modifier,
    assign_tier,
    calculate_lens_score,
    calculate_final_score,
)

__all__ = [
    "SCORING_DIMENSIONS", "LENS_WEIGHTS", "FINAL_LENS_WEIGHTS",
    "TIER_RULES", "MAX_TIER1_PER_RUN",
    "score_dimension", "apply_seasonal_modifier", "assign_tier",
    "calculate_lens_score", "calculate_final_score",
]
