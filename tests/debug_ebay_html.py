"""Debug: test the correct eBay selectors."""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)
from tools.costco_scraper import make_browser

ACTIVE_URL = "https://www.ebay.com/sch/i.html?_nkw=1+oz+gold+bar&_ipg=60"

with make_browser() as page:
    page.goto(ACTIVE_URL, timeout=25000, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(".s-card__price", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1500)

    prices = page.query_selector_all(".s-card__price")
    print(f".s-card__price count: {len(prices)}")
    for el in prices[:5]:
        print(f"  text: {el.inner_text()}")

    # Try count heading
    for sel in [".srp-controls__count-heading", ".srp-controls-v3"]:
        el = page.query_selector(sel)
        if el:
            print(f"\n{sel}: {el.inner_text()[:100]}")

    # Try extracting count from page text around "results"
    body = page.inner_text("body")[:3000]
    m = re.search(r"([\d,]+)\+?\s+results?", body, re.IGNORECASE)
    print(f"\nCount from body text: {m.group() if m else 'not found'}")
