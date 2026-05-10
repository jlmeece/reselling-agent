"""
Tool: status_logic
Status determination, transition rules, and reprice math.
Pure functions — no I/O, no API calls. Always testable.

Statuses
--------
PENDING       New product or Tier 1 scored — awaiting Jordan's review
APPROVED      Jordan approved — copy generating, verify stock before export
READY         Copy complete + stock OK — run ebay_export.py to list
ACTIVE        Live listing on eBay/website — top priority monitoring
WATCH         Tier 2 — promising, needs more data; may graduate to PENDING
PAUSED_OOS    Costco out of stock — daily stock-check until restocked
PAUSED_MARGIN Below margin threshold — re-eval when prices recover
PAUSED_DEMAND Low demand / high competition — deep research only, no scraping
PAUSED_SEASONAL Off-season — monthly re-eval
"""

# Statuses that the active monitor loop should process every run
ACTIVE_MONITOR_STATUSES = {"ACTIVE"}

# Statuses processed in the daily sweep (copy verify, stock check, margin recovery)
DAILY_SWEEP_STATUSES = {"APPROVED", "READY", "PAUSED_OOS", "PAUSED_MARGIN"}

# Statuses that trigger community research re-runs (weekly)
WATCH_STATUSES = {"WATCH"}

# Statuses the research loop handles (full scoring)
RESEARCH_STATUSES = {"PENDING"}

# Statuses that are completely skipped in all automated runs
SKIP_STATUSES = {"PAUSED_DEMAND", "PAUSED_SEASONAL"}


def determine_status(
    current_status,
    stock_status,
    margin_pct,
    price_changed,
    demand_score,
    ebay_url="",
    min_margin=0.10,
    min_demand=5,
):
    """
    Returns (new_status, reason_code, notes) for a product row.

    new_status:  one of the status constants above
    reason_code: short machine-readable tag for what changed (or "ok")
    notes:       human-readable explanation for sheet col T

    Only ACTIVE rows can be downgraded to PAUSED_* automatically.
    APPROVED/READY/WATCH are protected from auto-downgrade — human must approve.
    """
    notes = []
    reason_code = "ok"
    new_status = current_status  # default: no change

    # ── Auto-detect ACTIVE from eBay URL fill ────────────────────────
    if current_status == "READY" and ebay_url.startswith("http"):
        return "ACTIVE", "ebay_url_detected", "eBay listing URL detected — status set to ACTIVE"

    # ── ACTIVE monitoring ────────────────────────────────────────────
    if current_status == "ACTIVE":
        if stock_status == "OUT OF STOCK":
            reason_code = "oos"
            new_status = "PAUSED_OOS"
            notes.append("OUT OF STOCK — pause eBay listing immediately to avoid unfilled orders")
        elif stock_status == "Limited":
            reason_code = "low_stock"
            notes.append("Low stock at Costco — monitor closely, consider reducing listing quantity")
        elif stock_status == "CHECK FAILED":
            reason_code = "check_failed"
            notes.append("Could not reach Costco page — verify stock manually")

        if price_changed:
            reason_code = reason_code if reason_code != "ok" else "price_changed"
            notes.append("Costco price changed — recalculate margin, update eBay listing price")

        if margin_pct is not None and margin_pct < min_margin:
            if new_status == "ACTIVE":  # don't downgrade if already PAUSED_OOS
                new_status = "PAUSED_MARGIN"
                reason_code = "low_margin"
            notes.append(f"Margin {margin_pct:.1%} below {min_margin:.0%} threshold — not profitable at current prices")

        if demand_score is not None:
            try:
                if int(demand_score) < min_demand:
                    notes.append(f"Demand score {demand_score} is low — consider removing listing")
            except (ValueError, TypeError):
                pass

    # ── PAUSED_OOS: check if stock recovered ─────────────────────────
    elif current_status == "PAUSED_OOS":
        if stock_status not in ("OUT OF STOCK", "CHECK FAILED", "Limited"):
            new_status = "WATCH"
            reason_code = "restock"
            notes.append("Back in stock — moved to WATCH for re-research before re-listing")
        else:
            notes.append(f"Still {stock_status} — holding PAUSED_OOS")

    # ── PAUSED_MARGIN: check if prices recovered ─────────────────────
    elif current_status == "PAUSED_MARGIN":
        if margin_pct is not None and margin_pct >= min_margin:
            new_status = "WATCH"
            reason_code = "margin_recovered"
            notes.append(f"Margin recovered to {margin_pct:.1%} — moved to WATCH for re-research")
        else:
            mg = f"{margin_pct:.1%}" if margin_pct is not None else "unknown"
            notes.append(f"Margin still {mg} — holding PAUSED_MARGIN")

    # ── APPROVED: verify stock before promoting to READY ─────────────
    elif current_status == "APPROVED":
        if stock_status == "OUT OF STOCK":
            notes.append("Stock OOS — hold APPROVED until restocked before exporting")
        elif stock_status == "CHECK FAILED":
            notes.append("Could not verify stock — check Costco page manually before exporting")
        else:
            notes.append("Stock verified OK")

    return new_status, reason_code, " | ".join(notes) if notes else "All clear"


def suggest_reprice(new_cost, fee_rate, ship_cost=0, target_margin=0.20):
    """
    Returns suggested eBay listing price to maintain target_margin on cost.

    Formula: price = (new_cost + ship_cost) / (1 - fee_rate - target_margin)
    Rounds to nearest X.99 (e.g. $149.99).

    Returns None if inputs are invalid.
    """
    try:
        denominator = 1 - float(fee_rate) - target_margin
        if denominator <= 0:
            return None
        raw = (float(new_cost) + float(ship_cost)) / denominator
        return round(raw + 0.50, 0) - 0.01
    except (ValueError, ZeroDivisionError, TypeError):
        return None
