"""
Tool: ebay_research
Scrapes eBay sold/completed listings for real market data.
Uses the same CDP-connected Chrome as costco_scraper — pass the active page in.
Real Chrome avoids eBay's bot detection that blocks headless browsers.

Usage:
    from tools.costco_scraper import make_browser
    from tools.ebay_research import get_ebay_comps

    with make_browser() as page:
        comps = get_ebay_comps("product title", "Jewelry", page=page)

Returns: {sold_90d, avg_sold_price, active_count, price_range, fee_rate, note}
"""

import re
import random
import statistics
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Words that hurt eBay match accuracy (filler, marketing, sizing units shared across products)
_NOISE = {
    "costco", "kirkland", "signature", "new", "set", "piece", "pc", "pcs",
    "with", "and", "for", "the", "of", "by", "in", "a", "an",
    "style", "brand", "premium", "deluxe", "quality",
}
_MAX_QUERY_TOKENS = 8


_EBAY_SOLD_URL = (
    "https://www.ebay.com/sch/i.html"
    "?_nkw={query}"
    "&LH_Complete=1"
    "&LH_Sold=1"
    "&_ipg=60"
    "&_sacat=0"
)

_EBAY_ACTIVE_URL = (
    "https://www.ebay.com/sch/i.html"
    "?_nkw={query}"
    "&_ipg=60"
    "&_sacat=0"
)


def _build_query(title):
    """
    Build a clean eBay search query from a Costco product title.
    - Strip punctuation that breaks eBay's tokenizer (hyphens, slashes, parens)
    - Drop noise words (Kirkland, Signature, marketing fillers)
    - Keep brand + distinguishing model/size/edition words
    - Cap at 8 tokens (eBay relevance drops past that)
    """
    # Replace separators with spaces, then strip remaining punctuation
    cleaned = re.sub(r"[\-\/\(\)\[\],:;]", " ", title.lower())
    cleaned = re.sub(r"[^\w\s\.]", " ", cleaned)   # keep alphanumerics + dot (for "10.5")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    words = [w for w in cleaned.split() if w and w not in _NOISE]
    # If we trimmed too much, fall back to original split
    if len(words) < 2:
        words = cleaned.split()

    return " ".join(words[:_MAX_QUERY_TOKENS])


def _parse_price(text):
    """
    Extract a representative dollar amount from a price cell.
    eBay shows ranges like '$199.99 to $249.99' — we return the midpoint.
    Single-price cells return that price.
    """
    nums = re.findall(r"\$?([\d,]+\.?\d{0,2})", text)
    vals = []
    for n in nums:
        try:
            v = float(n.replace(",", ""))
            if v >= 1:
                vals.append(v)
        except ValueError:
            pass
    if not vals:
        return None
    if len(vals) >= 2 and ("to" in text.lower() or "—" in text or "–" in text):
        return round((vals[0] + vals[1]) / 2, 2)
    return vals[0]


def _filter_outliers(prices):
    """
    Trim the bottom and top 10% of prices to remove junk listings (loose parts,
    bundles, premium variants). Returns the trimmed list. Median-aware: drops
    anything below 0.3× median or above 3× median as a safety net.
    """
    if len(prices) < 4:
        return prices
    s = sorted(prices)
    trim = max(1, len(s) // 10)
    s = s[trim:-trim]
    if not s:
        return prices
    med = statistics.median(s)
    return [p for p in s if 0.3 * med <= p <= 3.0 * med] or s


def _scrape_ebay_page(page, url, label):
    """Navigate to an eBay URL and extract result count + prices."""
    count = None
    prices = []

    try:
        page.goto(url, timeout=25000, wait_until="domcontentloaded",
                  referer="https://www.ebay.com/")

        # Wait for price elements to render (eBay loads them via JS)
        try:
            page.wait_for_selector(".s-card__price", timeout=8000)
        except PlaywrightTimeout:
            pass

        page.wait_for_timeout(800 + random.randint(200, 400))

        # Result count — eBay has rotated this selector multiple times; try several
        for sel in (
            ".srp-controls__count-heading",
            "h1.srp-controls__count-heading",
            "[data-testid='srp-list-results-count']",
            ".result-count__count-heading",
        ):
            el = page.query_selector(sel)
            if el:
                raw = el.inner_text()
                m = re.search(r"([\d,]+)\+?", raw)
                if m:
                    count = min(int(m.group(1).replace(",", "")), 99999)
                    break

        # Fallback: body text
        if count is None:
            body_text = page.inner_text("body")[:3000]
            m = re.search(r"([\d,]+)\+?\s+results?", body_text, re.IGNORECASE)
            if m:
                count = min(int(m.group(1).replace(",", "")), 99999)

        # If eBay says "0 results" any prices on the page are "you might also like"
        # suggestions, not real comps. Drop them.
        if count == 0:
            return count, []

        # Prices — eBay now uses .s-card__price
        for el in page.query_selector_all(".s-card__price")[:60]:
            p = _parse_price(el.inner_text())
            if p and p > 1:
                prices.append(p)

    except PlaywrightTimeout:
        logger.debug(f"  eBay {label} timed out")
    except Exception as e:
        logger.debug(f"  eBay {label} error: {e}")

    return count, prices


def get_ebay_comps(product_title, category=None, page=None):
    """
    Searches eBay sold and active listings.

    page: an active Playwright page (CDP Chrome). Pass this from make_browser()
          so eBay sees real Chrome. If None, falls back to headless (less reliable).

    Returns: {sold_90d, avg_sold_price, active_count, price_range, fee_rate, note}
    """
    import os, yaml
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "categories.yaml")
    with open(config_path) as f:
        categories = yaml.safe_load(f)["categories"]

    fee_rate = 0.1325
    if category and category in categories:
        fee_rate = categories[category].get("fee_rate", 0.1325)

    query     = _build_query(product_title)
    sold_url  = _EBAY_SOLD_URL.format(query=query.replace(" ", "+"))
    active_url = _EBAY_ACTIVE_URL.format(query=query.replace(" ", "+"))

    result = {
        "sold_90d":         None,   # int — count of sold listings (last ~90 days)
        "median_sold":      None,   # float — median sold price (outliers trimmed)
        "avg_sold_price":   None,   # float — mean sold price (outliers trimmed)
        "sold_range":       None,   # str — "$X–$Y" of trimmed sold range
        "active_count":     None,   # int — competing active listings
        "median_active":    None,   # float — median active asking price
        "price_basis":      None,   # str — "sold" | "active" | "none"
        "fee_rate":         fee_rate,
        "query_used":       query,
        "note":             "",
    }

    def _run(pg):
        sold_count, sold_prices_raw = _scrape_ebay_page(pg, sold_url, "sold")
        result["sold_90d"] = sold_count

        sold_prices = _filter_outliers(sold_prices_raw)
        if sold_prices:
            result["median_sold"]    = round(statistics.median(sold_prices), 2)
            result["avg_sold_price"] = round(sum(sold_prices) / len(sold_prices), 2)
            result["sold_range"]     = f"${min(sold_prices):.0f}–${max(sold_prices):.0f}"
            result["price_basis"]    = "sold"
        elif sold_count is None:
            result["note"] += "Sold count missing — likely needs eBay login in CostcoAgentProfile. "

        pg.wait_for_timeout(1200 + random.randint(200, 500))

        active_count, active_prices_raw = _scrape_ebay_page(pg, active_url, "active")
        result["active_count"] = active_count

        active_prices = _filter_outliers(active_prices_raw)
        if active_prices:
            result["median_active"] = round(statistics.median(active_prices), 2)

        # If no sold data, surface active median as the best-available basis
        # but DON'T overwrite avg_sold_price silently.
        if result["price_basis"] is None and active_prices:
            result["price_basis"] = "active"
            result["note"] += "Sold prices unavailable — using active asking prices as price reference. "

        if result["price_basis"] is None:
            result["price_basis"] = "none"
            result["note"] += "No sold or active price data found. "

        try:
            pg.goto("https://www.ebay.com", timeout=10000, wait_until="domcontentloaded")
        except Exception:
            pass

    if page is not None:
        _run(page)
    else:
        # Fallback: own headless browser (less reliable but works for testing)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,   # visible reduces bot detection
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            try:
                _run(ctx.new_page())
            finally:
                browser.close()

    result["note"] = "OK" if not result["note"] else result["note"].strip()

    logger.info(
        f"  eBay comps [{query}]: "
        f"sold≈{result['sold_90d']} ({result['price_basis']}) | "
        f"median=${result['median_sold'] or result['median_active']} | "
        f"active≈{result['active_count']}"
    )
    return result
