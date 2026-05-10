"""
Tests eBay research + community signals in isolation — no browser/sheet needed.
Run: python tests/test_ebay_and_community.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

import yaml
from tools.ebay_research import get_ebay_comps
from tools.community_signals import get_community_signals
from tools.costco_scraper import make_browser

config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "categories.yaml")
with open(config_path) as f:
    config = yaml.safe_load(f)

# ── Test 1: eBay comps ──────────────────────────────────────────
print("=" * 60)
print("TEST 1: eBay research — Costco 1oz Gold Bar")
print("=" * 60)
with make_browser() as chrome_page:
    ebay = get_ebay_comps(
        "Costco Kirkland Signature 1 oz Gold Bar .9999 Fine",
        "Jewelry",
        page=chrome_page,
    )
print(f"  Query used:      {ebay['query_used']}")
print(f"  Sold ~90d:       {ebay['sold_90d']}")
print(f"  Avg sold price:  ${ebay['avg_sold_price']}")
print(f"  Active listings: {ebay['active_count']}")
print(f"  Price range:     {ebay['price_range']}")
print(f"  Fee rate:        {ebay['fee_rate']}")
print(f"  Note:            {ebay['note']}")

# ── Test 2: Community signals ───────────────────────────────────
print()
print("=" * 60)
print("TEST 2: Community signals — Jewelry/Gold category")
print("=" * 60)
cat_config = config["categories"]["Jewelry"]
signals = get_community_signals(
    "Costco Kirkland Signature 1 oz Gold Bar",
    "Jewelry",
    cat_config,
)
print(f"  Signal strength: {signals['signal_strength']}/10")
print(f"  Active sources:  {signals['active_sources']}")
print(f"  Stale sources:   {signals['stale_sources']}")
print(f"  Summary: {signals['summary']}")
print()
if signals["recent_posts"]:
    print("  Recent posts:")
    for p in signals["recent_posts"][:3]:
        print(f"    [{p['days_ago']}d ago | score {p['score']}] r/{p['subreddit']}: {p['title'][:70]}")

print("\nTests complete.")
