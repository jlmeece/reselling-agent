"""
Test the research pipeline in isolation — no sheet reads, no browser.
Calls Claude API with a fake product and prints the full Tier result.
Run from project root: python tests/test_researcher.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

import yaml
from agents.researcher import _run_claude_research
from tools.tier_scorer import score_product
from skills.research_gold import run_pass3 as gold_pass3

# Load real category config
config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "categories.yaml")
with open(config_path) as f:
    config = yaml.safe_load(f)
cat_config = config["categories"]["Jewelry"]

# Test product
TITLE       = "Costco Kirkland Signature 1 oz Gold Bar .9999 Fine"
CATEGORY    = "Jewelry"
COSTCO_COST = "2450.00"
EBAY_PRICE  = "2650.00"
STOCK       = "In Stock"
FEE_RATE    = cat_config["fee_rate"]

print(f"Running Claude research for: {TITLE}")
print("-" * 60)

result = _run_claude_research(TITLE, CATEGORY, COSTCO_COST, EBAY_PRICE, STOCK, FEE_RATE, cat_config.get("notes", ""))

print("\n--- Dimension Scores ---")
for dim, score in result["dimension_scores"].items():
    print(f"  {dim}: {score}")

print("\n--- 4-Lens Reasoning ---")
for lens, text in result["reasoning"].items():
    print(f"  {lens.replace('_', ' ').title()}: {text}")

print(f"\n--- eBay Estimates ---")
print(f"  Sold 90d:  {result.get('estimated_sold_90d')}")
print(f"  Avg price: {result.get('estimated_avg_price')}")
print(f"  Comp count: {result.get('estimated_comp_count')}")
print(f"\n--- Search Terms ---")
for t in result.get("search_terms", []):
    print(f"  {t}")

# Now run Pass 3 scoring math
avail_map = {"In Stock": 10, "Limited": 6, "OUT OF STOCK": 0, "Unknown": 3}
result["dimension_scores"]["costco_availability"] = avail_map.get(STOCK, 3)

pass3 = gold_pass3(result["dimension_scores"], cat_config, reasoning=result["reasoning"])

print(f"\n{'='*60}")
print(f"TIER: {pass3['tier']}  |  SCORE: {pass3['weighted_score']}")
print(f"Recommendation: {pass3['recommendation']}")
print(f"\n--- Lens Scores ---")
for lens, score in pass3["lens_scores"].items():
    print(f"  {lens.replace('_', ' ').title()}: {score}")
print(f"{'='*60}")
print("\nTest PASSED — research pipeline is working.")
