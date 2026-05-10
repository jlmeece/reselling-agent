"""
Skill: scoring
Thin re-export of the shared base_scoring library.

The canonical scoring engine lives at:
    C:\\Users\\jorda\\.claude\\skills\\base_scoring.py

All projects import from there. This file exists so that:
  - Existing project imports (from skills.scoring import ...) keep working
  - The project doesn't duplicate scoring logic that belongs to the shared library
  - When the shared scoring engine is updated, this project gets it automatically

To update scoring thresholds or add new dimensions: edit base_scoring.py, not this file.
"""

import sys
import os

_SHARED_SKILLS = r"C:\Users\jorda\.claude\skills"
if _SHARED_SKILLS not in sys.path:
    sys.path.insert(0, _SHARED_SKILLS)

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
