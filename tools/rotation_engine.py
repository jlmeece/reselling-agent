"""
Tool: rotation_engine
Computes composite performance scores and identifies products that should be
rotated out when a category reaches capacity.

Performance score (0–10):
  margin_score   * 4.0  — are we hitting profit goals?
  demand_score   * 4.0  — is there eBay market demand?
  velocity_score * 2.0  — are we actually moving units?

Pure functions where possible — rotation check writes to sheet via caller.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from loguru import logger


# Statuses that count toward a category's "active" capacity
ACTIVE_STATUSES = {"ACTIVE", "READY"}

# Statuses that are candidates to replace an underperformer
CANDIDATE_STATUSES = {"PENDING", "WATCH"}


# ── Scoring ───────────────────────────────────────────────────────────────────

def compute_perf_score(
    margin_pct: float | None,
    demand_score: float | None,
    units_sold: float | None,
    target_margin: float = 0.20,
    velocity_target: float = 2.0,
) -> float:
    """
    Returns a 0–10 composite performance score.

    margin_pct:      net margin as a decimal (e.g. 0.22 for 22%)
    demand_score:    0–10 score from the research pipeline (col B)
    units_sold:      total units sold (col U); 0 if not yet tracked
    target_margin:   the margin % that earns full margin points (default 20%)
    velocity_target: units/period to earn full velocity points (default 2)
    """
    # Margin component (0–1.5, capped)
    if margin_pct is not None and target_margin > 0:
        margin_norm = min(margin_pct / target_margin, 1.5)
    else:
        margin_norm = 0.0

    # Demand component (0–1.0)
    if demand_score is not None:
        demand_norm = min(float(demand_score) / 10.0, 1.0)
    else:
        demand_norm = 0.0

    # Velocity component (0–1.5, capped)
    if units_sold is not None and velocity_target > 0:
        velocity_norm = min(float(units_sold) / velocity_target, 1.5)
    else:
        velocity_norm = 0.0

    score = margin_norm * 4.0 + demand_norm * 4.0 + velocity_norm * 2.0
    return round(min(score, 10.0), 2)


# ── Column helpers ────────────────────────────────────────────────────────────

def _col_to_idx(col_letter: str) -> int:
    result = 0
    for c in col_letter.upper():
        result = result * 26 + (ord(c) - ord("A") + 1)
    return result - 1


def _safe(lst, i, default=""):
    return str(lst[i]).strip() if i < len(lst) else default


def _parse_float(val, default=None):
    try:
        return float(str(val).replace("$", "").replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return default


# ── Rotation check ────────────────────────────────────────────────────────────

def find_rotation_candidates(
    category_name: str,
    rows_with_idx: list[tuple[int, list]],
    col_map: dict,
    cat_config: dict,
    business_config: dict,
) -> list[dict]:
    """
    For a single category, find products that should be rotated out.

    Returns list of dicts: {title, row, perf_score, reason, suggested_replacement}
    Only returns candidates when the category is at or above capacity.
    """
    max_active        = cat_config.get("max_active", 20)
    capacity_headroom = cat_config.get("capacity_headroom", 3)
    rotation_threshold = cat_config.get("rotation_threshold", 4.0)
    velocity_target   = cat_config.get("velocity_target", 2.0)
    target_margin     = business_config.get("min_margin_threshold", 0.10) * 2  # 2× min = target

    idx = {k: _col_to_idx(v) for k, v in col_map.items()}

    active_rows    = []
    candidate_rows = []

    for sheet_row, row in rows_with_idx:
        status   = _safe(row, idx["status"])
        cat      = _safe(row, idx["category"])
        if cat != category_name:
            continue

        title        = _safe(row, idx["title"])
        demand_raw   = _parse_float(_safe(row, idx["demand_score"]))
        margin_raw   = _parse_float(_safe(row, idx["net_margin"]))  # J = formula col
        units_raw    = _parse_float(_safe(row, idx["units_sold"]), default=0)
        score        = compute_perf_score(margin_raw, demand_raw, units_raw,
                                          target_margin=target_margin,
                                          velocity_target=velocity_target)

        if status in ACTIVE_STATUSES:
            active_rows.append({
                "title": title, "row": sheet_row,
                "perf_score": score,
                "margin": margin_raw, "demand": demand_raw, "units": units_raw,
            })
        elif status in CANDIDATE_STATUSES:
            candidate_rows.append({
                "title": title, "row": sheet_row,
                "perf_score": score,
            })

    # Only trigger rotation if category is at or above soft cap
    effective_cap = max_active - capacity_headroom
    if len(active_rows) < effective_cap:
        return []

    # Find underperformers
    underperformers = [p for p in active_rows if p["perf_score"] < rotation_threshold]
    if not underperformers:
        return []

    # Sort candidates descending by score — best replacement first
    candidate_rows.sort(key=lambda x: x["perf_score"], reverse=True)
    best_candidate = candidate_rows[0] if candidate_rows else None

    results = []
    for p in sorted(underperformers, key=lambda x: x["perf_score"]):
        parts = []
        if p["margin"] is not None and p["margin"] < target_margin:
            parts.append(f"margin {p['margin']:.1%} below target {target_margin:.1%}")
        if p["demand"] is not None and p["demand"] < 5:
            parts.append(f"demand score {p['demand']:.1f}/10")
        if p["units"] == 0:
            parts.append("no units sold tracked")
        reason = "; ".join(parts) if parts else "score below rotation threshold"

        results.append({
            "title":       p["title"],
            "row":         p["row"],
            "perf_score":  p["perf_score"],
            "reason":      reason,
            "suggested_replacement": best_candidate,
        })

    return results


def run_rotation_check(
    service,
    sheet_name: str,
    all_data: list[list],
    col_map: dict,
    config: dict,
    start_row: int = 4,
) -> dict[str, list]:
    """
    Runs rotation check across all categories.
    Writes perf_score (col AU) and rotation note (col T) to flagged rows.
    Returns {category_name: [candidates]} — only categories with candidates.

    Also writes perf_score for ALL active products (not just candidates) so the
    rotation digest can show healthy categories too.
    """
    from tools.sheet_writer import write_row_partial

    business   = config["business"]
    categories = config["categories"]
    idx        = {k: _col_to_idx(v) for k, v in col_map.items()}

    rows_with_idx = [
        (i + start_row, row)
        for i, row in enumerate(all_data)
        if row
    ]

    rotation_results: dict[str, list] = {}

    # First pass: write perf_score for all ACTIVE/READY products
    for sheet_row, row in rows_with_idx:
        status = _safe(row, idx["status"])
        if status not in ACTIVE_STATUSES:
            continue

        cat_name    = _safe(row, idx["category"])
        cat_config  = categories.get(cat_name, {})
        velocity_target = cat_config.get("velocity_target", 2.0)
        target_margin   = business.get("min_margin_threshold", 0.10) * 2

        demand_raw  = _parse_float(_safe(row, idx["demand_score"]))
        margin_raw  = _parse_float(_safe(row, idx["net_margin"]))
        units_raw   = _parse_float(_safe(row, idx["units_sold"]), default=0)
        score       = compute_perf_score(margin_raw, demand_raw, units_raw,
                                         target_margin=target_margin,
                                         velocity_target=velocity_target)

        write_row_partial(service, sheet_name, sheet_row, [(col_map["perf_score"], score)])

    # Second pass: rotation candidates per category
    for cat_name, cat_config in categories.items():
        if not cat_config.get("max_active"):
            continue

        candidates = find_rotation_candidates(
            cat_name, rows_with_idx, col_map, cat_config, business
        )

        if candidates:
            rotation_results[cat_name] = candidates
            for c in candidates:
                replacement = c["suggested_replacement"]
                if replacement:
                    note = (
                        f"ROTATION CANDIDATE (score: {c['perf_score']}) — "
                        f"{cat_name} at capacity. Reason: {c['reason']}. "
                        f"Consider replacing with: \"{replacement['title']}\" "
                        f"(score: {replacement['perf_score']})."
                    )
                else:
                    note = (
                        f"ROTATION CANDIDATE (score: {c['perf_score']}) — "
                        f"{cat_name} at capacity. Reason: {c['reason']}. "
                        f"No PENDING/WATCH replacement available yet."
                    )
                write_row_partial(service, sheet_name, c["row"], [
                    (col_map["notes"], note),
                ])
                logger.info(f"  Rotation candidate: {c['title'][:50]} (score: {c['perf_score']})")

    return rotation_results


# ── Rotation log ──────────────────────────────────────────────────────────────

def save_rotation_log(rotation_results: dict[str, list]):
    """Append today's rotation check to data/rotation_log.json."""
    log_path = Path(__file__).parent.parent / "data" / "rotation_log.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []

    entry = {
        "date": date.today().isoformat(),
        "categories": {
            cat: [{"title": c["title"], "row": c["row"], "perf_score": c["perf_score"]} for c in candidates]
            for cat, candidates in rotation_results.items()
        },
    }
    existing.append(entry)
    # Keep last 90 entries
    log_path.write_text(json.dumps(existing[-90:], indent=2), encoding="utf-8")
