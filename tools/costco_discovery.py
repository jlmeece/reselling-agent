"""
Tool: costco_discovery
Discovers new products on Costco category pages by intercepting the internal
search/catalog API (gdx-api.costco.com) that Costco's React SPA calls automatically.

This is more reliable than DOM scraping because:
  - Products are fetched from a clean JSON API, not rendered HTML
  - Works regardless of Costco's frontend changes
  - Captures all products in one response without scrolling

Usage:
    with make_browser() as page:
        products = discover_all(page, categories_config)

Returns: list of {title, url, price, category}
"""

import os
import json
import time
import random
from loguru import logger
from playwright.sync_api import TimeoutError as PlaywrightTimeout

PERFORMANCE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "category_performance.json"
)


def _load_performance():
    if not os.path.exists(PERFORMANCE_FILE):
        return {}
    with open(PERFORMANCE_FILE) as f:
        return json.load(f)

COSTCO_SEARCH_API = "gdx-api.costco.com/catalog/search/api/v1/search"

# Title-based allowlist for categories where keyword searches return noisy results.
# If a category has title_keywords configured, discovered products must match at least one.
CATEGORY_TITLE_FILTERS = {
    "Precious Metals": [
        "gold bar", "silver bar", "gold coin", "silver coin",
        "pamp", "argor", "rand refinery", "royal canadian mint",
        "platinum bar", "palladium", "maple leaf", "american gold eagle",
        "krugerrand", "oz gold", "oz silver", "gram gold", "gram pure gold",
        "gold bullion", "silver bullion",
    ],
}


def _passes_title_filter(title: str, category_name: str) -> bool:
    keywords = CATEGORY_TITLE_FILTERS.get(category_name)
    if not keywords:
        return True  # no filter defined — accept everything
    t = title.lower()
    return any(kw in t for kw in keywords)


def _extract_products_from_api(api_data, category_name):
    """Parse the Costco search API JSON response into product dicts."""
    products = []
    try:
        results = api_data.get("searchResult", {}).get("results", [])
        for item in results:
            p = item.get("product", {})
            title = p.get("title", "").strip()
            uri   = p.get("uri", "").strip()
            if not title or not uri:
                continue
            if not _passes_title_filter(title, category_name):
                continue
            if not uri.startswith("http"):
                uri = "https://www.costco.com" + uri
            products.append({
                "title":    title,
                "url":      uri.split("?")[0],
                "price":    None,   # not in API — scraped from product page later
                "category": category_name,
            })
    except Exception as e:
        logger.debug(f"  API parse error: {e}")
    return products


def discover_category(page, discovery_url, category_name):
    """
    Navigates to one Costco category page and captures the product API response.
    Returns list of {title, url, price, category}.
    """
    products = []
    api_responses = []

    def on_response(response):
        if COSTCO_SEARCH_API in response.url:
            try:
                api_responses.append(response.json())
            except Exception:
                pass

    page.on("response", on_response)

    try:
        resp = page.goto(
            discovery_url, timeout=30000, wait_until="domcontentloaded",
            referer="https://www.costco.com/",
        )
        if resp and resp.status >= 400:
            logger.warning(f"  Category page blocked (HTTP {resp.status}): {discovery_url}")
            return products

        # Wait for the API call to fire and return
        page.wait_for_timeout(4000 + random.randint(500, 1000))

        # Scroll to trigger any lazy-loaded batches
        for pos in [500, 1500, 3000]:
            page.evaluate(f"window.scrollTo(0, {pos})")
            page.wait_for_timeout(500)
        page.wait_for_timeout(1000)

        # Parse all captured API responses (may be multiple pages)
        for api_data in api_responses:
            batch = _extract_products_from_api(api_data, category_name)
            products.extend(batch)

        # Dedupe by URL
        seen = set()
        unique = []
        for p in products:
            if p["url"] not in seen:
                seen.add(p["url"])
                unique.append(p)
        products = unique

        logger.info(f"  Found {len(products)} products on {discovery_url}")

    except PlaywrightTimeout:
        logger.warning(f"  Timed out: {discovery_url}")
    except Exception as e:
        logger.warning(f"  Error on {discovery_url}: {e}")
    finally:
        page.remove_listener("response", on_response)

    return products


def discover_all(page, categories_config, category_filter=None):
    """
    Runs discovery across active category discovery URLs.
    Pass category_filter (e.g. 'Precious Metals') to scrape only that category.
    Categories are sorted by historical performance score (high performers first)
    so the most promising categories are always discovered before lower ones.
    Returns deduplicated list of all discovered products.
    """
    all_products = []
    seen_urls = set()

    performance = _load_performance()

    # Sort categories: higher performance_score first, new categories default to 0.5
    sorted_categories = sorted(
        categories_config.items(),
        key=lambda kv: performance.get(kv[0], {}).get("performance_score", 0.5),
        reverse=True,
    )

    order = [name for name, cat in sorted_categories if cat.get("discovery_urls")]
    if order:
        logger.info(f"Discovery order (by performance): {' → '.join(order)}")

    for category_name, cat in sorted_categories:
        # Respect category filter — skip other categories entirely (no wasted scraping)
        if category_filter and category_name != category_filter:
            continue

        discovery_urls = cat.get("discovery_urls", [])
        if not discovery_urls:
            continue

        # Per-category cap: never flood the sheet with hundreds of products.
        # Precious Metals has ~15 real products; most categories top out at ~50.
        max_per_category = cat.get("max_discovery", 60)
        cat_count = 0

        for discovery_url in discovery_urls:
            if cat_count >= max_per_category:
                logger.info(f"  {category_name}: reached {max_per_category}-product cap — stopping discovery for this category")
                break
            products = discover_category(page, discovery_url, category_name)
            for p in products:
                if p["url"] not in seen_urls and cat_count < max_per_category:
                    seen_urls.add(p["url"])
                    all_products.append(p)
                    cat_count += 1
            time.sleep(2 + random.uniform(0.5, 1.5))

    logger.info(f"Discovery complete — {len(all_products)} unique products across all categories.")
    return all_products
