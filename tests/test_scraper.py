"""
Quick test: discover one real product URL then scrape it.
"""
import sys
sys.path.insert(0, ".")
from tools.costco_scraper import make_browser, scrape_costco
from tools.costco_discovery import discover_category

DISCOVERY_URL = "https://www.costco.com/precious-metals.html"

with make_browser() as page:
    print("Discovering products on precious-metals page...")
    products = discover_category(page, DISCOVERY_URL, "Jewelry")
    print(f"Found {len(products)} products")

    if not products:
        print("No products found — check discovery or try a different category URL.")
        sys.exit(1)

    # Test scrape on the first result
    target = products[0]
    print(f"\nScraping: {target['title']}")
    print(f"URL: {target['url']}")

    result = scrape_costco(target["url"], page=page)

print()
print("=== SCRAPE RESULT ===")
print(f"Price:   {result['price']}")
print(f"Stock:   {result['stock_status']}")
print(f"Title:   {result['title']}")
print(f"Images ({len(result['image_urls'])}):")
for url in result["image_urls"]:
    print(f"  {url}")
if result["error"]:
    print(f"Error:   {result['error']}")
