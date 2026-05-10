"""
Diagnose the price issue:
1. Check if the discovery API response includes price
2. Check what price selectors are present on a live product page
3. Check if waiting longer / using networkidle gets the price
"""
import sys, json
sys.path.insert(0, ".")
from tools.costco_scraper import make_browser, COOKIES_PATH
from playwright.sync_api import TimeoutError as PlaywrightTimeout

PRODUCT_URL = "https://www.costco.com/p/-/10-gram-gold-bar-argor-heraeus-small-craft-new-in-assay/4000412466"

api_responses = []

def on_response(response):
    if "gdx-api.costco.com" in response.url or "api.costco.com" in response.url:
        try:
            data = response.json()
            api_responses.append({"url": response.url, "data": data})
        except Exception:
            pass

with make_browser() as page:
    page.on("response", on_response)
    print(f"Navigating to product page...")
    page.goto(PRODUCT_URL, timeout=30000, wait_until="domcontentloaded")

    # Wait longer for JS to render price
    print("Waiting for price element (up to 10s)...")
    try:
        page.wait_for_selector("[itemprop='price'], .e-price-display, [class*='price']", timeout=10000)
        print("  Price element appeared!")
    except PlaywrightTimeout:
        print("  Price element never appeared in 10s")

    # Try all price-related selectors
    selectors = [
        "[itemprop='price']",
        ".e-price-display",
        "[class*='price']",
        "[class*='Price']",
        "[data-automation*='price']",
        "span[class*='value']",
    ]
    print("\nSelector scan:")
    for sel in selectors:
        els = page.query_selector_all(sel)
        for el in els[:2]:
            try:
                txt = (el.get_attribute("content") or el.inner_text() or "").strip()
                if txt and len(txt) < 40:
                    print(f"  {sel}: '{txt}'")
            except Exception:
                pass

    # Check API responses captured during page load
    print(f"\nAPI responses captured: {len(api_responses)}")
    for r in api_responses[:3]:
        print(f"  URL: {r['url']}")
        # Look for price in the response
        raw = json.dumps(r["data"])
        if "price" in raw.lower():
            print("  Contains 'price' key — extracting...")
            def find_prices(obj, path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if "price" in k.lower():
                            print(f"    {path}.{k} = {v}")
                        find_prices(v, f"{path}.{k}")
                elif isinstance(obj, list):
                    for i, v in enumerate(obj[:3]):
                        find_prices(v, f"{path}[{i}]")
            find_prices(r["data"])
