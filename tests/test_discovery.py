"""Test Costco product discovery. Run: python tests/test_discovery.py"""
import sys, os, yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)
from tools.costco_scraper import make_browser
from tools.costco_discovery import discover_all

with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "categories.yaml")) as f:
    config = yaml.safe_load(f)

print("Starting Costco discovery...")
with make_browser() as page:
    products = discover_all(page, config["categories"])

print(f"\nTotal products found: {len(products)}")
by_cat = {}
for p in products:
    by_cat.setdefault(p["category"], []).append(p)

for cat, items in by_cat.items():
    print(f"\n{cat}: {len(items)} products")
    for p in items[:5]:
        print(f"  {p['title'][:60]}")
        print(f"    {p['url']}")

print("\nTest complete.")
