"""Find the correct Costco URL for gold bars/coins."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)
from tools.costco_scraper import make_browser
from tools.costco_discovery import discover_category

GOLD_URLS = [
    "https://www.costco.com/CatalogSearch?keyword=gold+bar",
    "https://www.costco.com/CatalogSearch?keyword=gold+bar+coin",
    "https://www.costco.com/precious-metals.html",
    "https://www.costco.com/investment-grade-gold.html",
]

with make_browser() as page:
    for url in GOLD_URLS:
        products = discover_category(page, url, "Jewelry")
        print(f"\n{url}")
        print(f"  Products found: {len(products)}")
        for p in products[:8]:
            print(f"  - {p['title']}")
