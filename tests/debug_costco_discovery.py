"""Debug: find price field in Costco API, map all category pages."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)
from tools.costco_scraper import make_browser

# Map all working category pages we need
CATEGORY_PAGES = [
    ("Jewelry", "https://www.costco.com/gold-bracelets.html"),
    ("Jewelry", "https://www.costco.com/gold-earrings.html"),
    ("Jewelry", "https://www.costco.com/gold-necklaces.html"),
    ("Jewelry", "https://www.costco.com/rings.html"),
    ("Outdoor Furniture", "https://www.costco.com/patio-furniture.html"),
]

with make_browser() as page:
    # First: find where price lives in the product object
    captured = {}
    page.on("response", lambda r: captured.update({"data": r.json()})
            if "gdx-api.costco.com/catalog/search/api/v1/search" in r.url else None)

    page.goto("https://www.costco.com/gold-bracelets.html", timeout=20000,
              wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    if "data" in captured:
        items = captured["data"]["searchResult"]["results"]
        p = items[0]["product"]
        print("=== Price field investigation ===")
        print(f"title: {p.get('title')}")
        print(f"uri: {p.get('uri')}")

        # Check localInventories
        li = p.get("localInventories", [])
        if li:
            print(f"\nlocalInventories[0] keys: {list(li[0].keys())}")
            for k, v in li[0].items():
                if isinstance(v, (str, int, float)) and v:
                    print(f"  {k}: {v}")

        # Check attributes
        attrs = p.get("attributes", {})
        for k, v in list(attrs.items())[:10]:
            print(f"  attr {k}: {v}")

        # Check variants
        variants = p.get("variants", [])
        if variants:
            print(f"\nvariants[0] keys: {list(variants[0].keys())}")

        print(f"\n=== All 24 products (title + uri) ===")
        for item in items:
            pp = item["product"]
            print(f"  {pp.get('title', '?')[:60]:60s} | {pp.get('uri', '?')[:80]}")
