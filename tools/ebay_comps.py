"""
Tool: ebay_comps
Returns eBay market data for a product: fee rate, sold count, avg price, comp count.

Current state: returns fee rates from categories.yaml + placeholder comp data.
Fee rates are accurate and used for margin calculations.
Comp data (sold_90d, avg_price, comp_count) requires manual Terapeak research for now.

Phase 4 upgrade path: replace with live eBay Browse API calls once API key is approved.
Terapeak (free in eBay Seller Hub): Research → Terapeak → search by keyword.
"""

import os
import yaml


def _load_categories():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "categories.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)["categories"]


def get_ebay_comps(product_title, category):
    """
    Returns fee rate and placeholder comp data for a product.
    fee_rate is accurate — pulled from categories.yaml.
    Comp fields (avg_sold_price, sold_90d, comp_count) are filled manually via Terapeak.
    """
    categories = _load_categories()
    cat_config = categories.get(category, {})
    fee_rate = cat_config.get("fee_rate", 0.1325)

    return {
        "avg_sold_price": None,
        "sold_90d": None,
        "comp_count": None,
        "fee_rate": fee_rate,
        "note": "Use Terapeak in eBay Seller Hub to fill comp data manually.",
    }
