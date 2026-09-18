"""
MPT Engine — Phase 1: Sharpe Ranking

Ranks reselling products by risk-adjusted return (Sharpe ratio).
Pure Python stdlib — no external dependencies.
"""

import re

HURDLE_RATE = 0.25  # 25% annualized minimum acceptable return

CATEGORY_PRIOR_SIGMA = {
    "Precious Metals": 0.08,   # gold/silver prices are tight
    "Jewelry": 0.35,           # high variance — condition, style, karat
    "Watches": 0.25,
}
DEFAULT_PRIOR_SIGMA = 0.30


def _parse_sold_range(sold_range):
    """Parse '$X–$Y' or '$X-$Y' into (low, high) floats. Returns None if unparseable."""
    if not sold_range:
        return None
    cleaned = sold_range.replace("$", "").replace(",", "")
    # Handle en dash (–), em dash (—), or ASCII hyphen (-)
    parts = re.split(r"[–—\-]", cleaned, maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        low = float(parts[0].strip())
        high = float(parts[1].strip())
        if low > high:
            low, high = high, low
        return (low, high)
    except (ValueError, TypeError):
        return None


def _compute_mu(product):
    """Returns (annualized_return, days_to_sell_est)."""
    costco_cost = product.get("costco_cost") or 0.0
    ebay_price = product.get("ebay_price") or 0.0
    fee_rate = product.get("fee_rate") or 0.0
    ship_cost = product.get("ship_cost") or 0.0
    ad_cost = product.get("ad_cost") or 0.0
    sold_90d = product.get("sold_90d") or 0

    net_profit = ebay_price - costco_cost - (ebay_price * fee_rate) - ship_cost - ad_cost

    days_to_sell = 90 / max(sold_90d, 1) if sold_90d else 90.0
    days_to_sell_est = max(1, int(round(days_to_sell)))

    if costco_cost <= 0:
        return (0.0, days_to_sell_est)

    annualized_return = (net_profit / costco_cost) * (365 / days_to_sell)
    return (annualized_return, days_to_sell_est)


def _compute_sigma(product, mu):
    """Compute risk (price volatility) with Bayesian shrinkage toward category prior."""
    category = product.get("category", "")
    sold_90d = product.get("sold_90d") or 0
    avg_sold_price = product.get("avg_sold_price") or 0.0
    sold_range = product.get("sold_range")

    prior_sigma = CATEGORY_PRIOR_SIGMA.get(category, DEFAULT_PRIOR_SIGMA)

    parsed = _parse_sold_range(sold_range)
    if parsed and avg_sold_price > 0:
        low, high = parsed
        price_volatility = (high - low) / avg_sold_price
    else:
        price_volatility = 0.5  # medium uncertainty when no range data

    # Blend with category prior — full prior when <15 comps, full observed when 15+
    shrink_weight = min(sold_90d, 15) / 15.0
    sigma = (shrink_weight * price_volatility) + ((1 - shrink_weight) * prior_sigma)

    return sigma


def sharpe_label(sharpe):
    """Return a human-readable Sharpe label for the dashboard."""
    if sharpe >= 2.0:
        return "🔥 Strong"
    elif sharpe >= 1.0:
        return "✅ Good"
    elif sharpe >= 0.0:
        return "⚠️ Marginal"
    else:
        return "❌ Below hurdle"


def rank_products(products):
    """
    Enrich each product with MPT metrics and return sorted by sharpe descending.

    Input keys: title, category, costco_cost, ebay_price, sold_90d, avg_sold_price,
                sold_range, active_count, fee_rate, ship_cost, ad_cost

    Added keys: mu, sigma, sharpe, days_to_sell_est, beats_hurdle, mpt_rank
    """
    results = []
    for p in products:
        product = dict(p)  # don't mutate caller's dict
        mu, days_to_sell_est = _compute_mu(product)
        sigma = _compute_sigma(product, mu)
        excess_return = mu - HURDLE_RATE
        sharpe = excess_return / max(sigma, 0.01)

        product["mu"] = round(mu, 4)
        product["sigma"] = round(sigma, 4)
        product["sharpe"] = round(sharpe, 4)
        product["days_to_sell_est"] = days_to_sell_est
        product["beats_hurdle"] = mu > HURDLE_RATE
        results.append(product)

    results.sort(key=lambda x: x["sharpe"], reverse=True)
    for rank, p in enumerate(results, 1):
        p["mpt_rank"] = rank

    return results


_SHARPE_LEAD_RE = re.compile(r"-?\d+\.?\d*")


def rank_rows_by_sharpe(rows):
    """
    rows: list of (sheet_row, cell_text) — cell_text is the mpt_sharpe column's
    raw cell content (e.g. "1.2345 🔥 Strong") or blank/None.

    Parses the leading float out of each cell, sorts descending, and assigns
    rank 1 = highest Sharpe. Rows with blank/unparseable cells are omitted
    (no rank assigned).

    Returns: list of (sheet_row, rank) for parseable rows only.
    """
    parsed = []
    for row, text in rows:
        if not text:
            continue
        m = _SHARPE_LEAD_RE.match(str(text).strip())
        if not m:
            continue
        try:
            sharpe = float(m.group())
        except ValueError:
            continue
        parsed.append((row, sharpe))

    parsed.sort(key=lambda x: x[1], reverse=True)
    return [(row, rank) for rank, (row, _sharpe) in enumerate(parsed, 1)]


if __name__ == "__main__":
    # Smoke test — run with: python tools/mpt_engine.py
    test_products = [
        {
            "title": "PAMP Suisse 1oz Gold Bar",
            "category": "Precious Metals",
            "costco_cost": 2400.0,
            "ebay_price": 2700.0,
            "sold_90d": 30,
            "avg_sold_price": 2680.0,
            "sold_range": "$2,600–$2,760",
            "active_count": 12,
            "fee_rate": 0.04,
            "ship_cost": 0.0,
            "ad_cost": 0.0,
        },
        {
            "title": "Costco Diamond Tennis Bracelet 14K",
            "category": "Jewelry",
            "costco_cost": 850.0,
            "ebay_price": 1100.0,
            "sold_90d": 8,
            "avg_sold_price": 1050.0,
            "sold_range": "$850–$1,200",
            "active_count": 45,
            "fee_rate": 0.15,
            "ship_cost": 12.0,
            "ad_cost": 0.0,
        },
    ]

    ranked = rank_products(test_products)
    print(f"Ranked {len(ranked)} products:\n")
    for p in ranked:
        label = sharpe_label(p["sharpe"])
        print(
            f"  [{p['mpt_rank']}] {p['title']}\n"
            f"      μ={p['mu']:.2%}  σ={p['sigma']:.2%}  "
            f"Sharpe={p['sharpe']:.2f}  days_to_sell={p['days_to_sell_est']}\n"
            f"      beats_hurdle={p['beats_hurdle']}  {label}\n"
        )
