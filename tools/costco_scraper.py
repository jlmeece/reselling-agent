"""
Tool: costco_scraper
Scrapes a Costco product page for price, stock status, and image URLs.
Returns a dict — never writes to the sheet directly.

Launches the user's real Chrome with a dedicated lightweight profile (no extensions,
no tabs), injects the saved Costco cookies, and connects via CDP.

Why this works:
  - Real Chrome binary → correct TLS fingerprint that passes Akamai
  - No --enable-automation flag → navigator.webdriver is never set
  - Valid Costco session cookies → authenticated session
  - Clean dedicated profile → debug port opens reliably every time

Prerequisites:
  - Run tools/setup_costco_session.py once to export Costco cookies from Chrome
  - Re-run when scraper starts returning 403s again (~30 days)
"""

import os
import re
import json
import time
import socket
import random
import subprocess
from datetime import datetime
from contextlib import contextmanager
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

CHROME_EXE       = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_PORT      = 9222
CHROME_DEBUG_URL = f"http://localhost:{CHROME_PORT}"
# Dedicated profile — no spaces in path, not on OneDrive, no extensions
AGENT_PROFILE    = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CostcoAgentProfile")
COOKIES_PATH     = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "costco_cookies.json"
)
# Tracks PID of Chrome the agent launched so we only kill our own process
CHROME_PID_FILE  = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "chrome_agent.pid"
)


def _debug_port_open():
    s = socket.socket()
    s.settimeout(1)
    ok = s.connect_ex(("127.0.0.1", CHROME_PORT)) == 0
    s.close()
    return ok


def _load_cookies():
    """Load Cookie-Editor exported JSON → Playwright cookie dicts.
    Warns clearly if session cookies are expired so the fix is obvious."""
    if not os.path.exists(COOKIES_PATH):
        logger.warning("  No cookies file — run: python tools/setup_costco_session.py")
        return []
    try:
        with open(COOKIES_PATH) as f:
            raw = json.load(f)
        same_site_map = {
            "strict": "Strict", "lax": "Lax", "none": "None",
            "no_restriction": "None", "unspecified": "Lax",
        }
        now = datetime.now().timestamp()
        expired_count = 0
        out = []
        for c in raw:
            if not c.get("name") or not c.get("value"):
                continue
            cookie = {
                "name":     c["name"],
                "value":    c["value"],
                "domain":   c.get("domain", ".costco.com"),
                "path":     c.get("path", "/"),
                "secure":   bool(c.get("secure", False)),
                "httpOnly": bool(c.get("httpOnly", c.get("http_only", False))),
                "sameSite": same_site_map.get((c.get("sameSite") or "lax").lower(), "Lax"),
            }
            exp = c.get("expirationDate") or c.get("expires")
            if exp and float(exp) > 0:
                cookie["expires"] = float(exp)
                if float(exp) < now:
                    expired_count += 1
            out.append(cookie)

        if expired_count > len(out) * 0.5:
            logger.error(
                f"  COOKIES EXPIRED ({expired_count}/{len(out)} expired) — "
                "run: python tools/setup_costco_session.py to refresh them. "
                "Scraper will likely return CHECK FAILED until cookies are updated."
            )
        else:
            logger.info(f"  Loaded {len(out)} Costco cookies ({expired_count} expired)")
        return out
    except Exception as e:
        logger.warning(f"  Failed to load cookies: {e}")
        return []


def _kill_agent_chrome():
    """Kill only the Chrome process the agent launched (by saved PID)."""
    if os.path.exists(CHROME_PID_FILE):
        try:
            pid = int(open(CHROME_PID_FILE).read().strip())
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
            os.remove(CHROME_PID_FILE)
            logger.info(f"  Killed agent Chrome (PID {pid})")
        except Exception as e:
            logger.debug(f"  PID kill failed ({e}) — falling back to profile-based kill")
            # Fallback: kill Chrome using the agent profile path as the discriminator
            subprocess.run(
                ["wmic", "process", "where",
                 f"name='chrome.exe' and commandline like '%CostcoAgentProfile%'",
                 "call", "terminate"],
                capture_output=True
            )
    time.sleep(2)


def _ensure_chrome():
    """
    Make sure Chrome is running on the debug port with the agent profile.
    Only kills the Chrome process the agent previously launched — never touches
    Chrome windows the user has open.
    """
    if _debug_port_open():
        logger.info("  Chrome already running on debug port.")
        return

    # Kill only our previously launched Chrome (by PID), not all chrome.exe
    _kill_agent_chrome()

    os.makedirs(AGENT_PROFILE, exist_ok=True)
    logger.info(f"  Launching Chrome (agent profile: {AGENT_PROFILE})...")
    proc = subprocess.Popen([
        CHROME_EXE,
        f"--remote-debugging-port={CHROME_PORT}",
        f"--remote-allow-origins=http://localhost:{CHROME_PORT}",
        f"--user-data-dir={AGENT_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
    ])

    # Save PID so we can kill only this process next time
    with open(CHROME_PID_FILE, "w") as f:
        f.write(str(proc.pid))

    for _ in range(20):
        time.sleep(1)
        if _debug_port_open():
            logger.info("  Chrome ready.")
            return
    raise RuntimeError("Chrome didn't open the debug port in 20s")


@contextmanager
def make_browser():
    """
    Connects to Chrome via CDP, injects Costco cookies, warms up on the homepage,
    and yields a ready page for scraping.

    Usage:
        with make_browser() as page:
            for url in urls:
                result = scrape_costco(url, page=page)
    """
    _ensure_chrome()
    cookies = _load_cookies()

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(CHROME_DEBUG_URL)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception as e:
                logger.warning(f"  Cookie injection failed: {e}")

        page = context.new_page()
        try:
            logger.info("  Warming up Costco session (homepage visit)...")
            page.goto("https://www.costco.com", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000 + random.randint(500, 1500))
            yield page
        finally:
            page.close()
            browser.close()  # CDP: disconnects without closing the remote Chrome
    finally:
        pw.stop()


def refresh_session(page):
    """
    Re-visits the Costco homepage to reset session cookies and keep auth alive.
    Call every ~20 products during long research runs to prevent CHECK FAILED.
    """
    try:
        logger.info("  Refreshing Costco session (homepage revisit)...")
        page.goto("https://www.costco.com", timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000 + random.randint(500, 1000))
    except Exception as e:
        logger.warning(f"  Session refresh failed (non-fatal): {e}")


# ── Brand + Model extraction ───────────────────────────────────────────────────

def _extract_brand(page):
    """
    Best-effort brand extraction. Tries (in order):
      1. schema.org meta itemprop="brand"
      2. visible "Brand:" label in product spec/info area
      3. JSON-LD <script type="application/ld+json"> brand field
    Returns string or None.
    """
    try:
        el = page.query_selector('meta[itemprop="brand"]')
        if el:
            v = (el.get_attribute("content") or "").strip()
            if v:
                return v[:60]
    except Exception:
        pass

    try:
        for sel in (
            "div.product-info-description-container",
            "div[class*='product-info']",
            "div[class*='product-details']",
            ".product-info-section",
            "body",
        ):
            container = page.query_selector(sel)
            if not container:
                continue
            text = container.inner_text()
            m = re.search(r"\bBrand\s*[:\-]\s*(.+)", text, re.IGNORECASE)
            if m:
                v = m.group(1).split("\n")[0].strip().strip(",.")
                if 2 <= len(v) <= 60:
                    return v
            break
    except Exception:
        pass

    try:
        scripts = page.query_selector_all('script[type="application/ld+json"]')
        for s in scripts:
            try:
                payload = json.loads(s.inner_text() or "{}")
            except Exception:
                continue
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                brand = item.get("brand") if isinstance(item, dict) else None
                if isinstance(brand, dict):
                    name = brand.get("name")
                    if name:
                        return str(name)[:60]
                elif isinstance(brand, str) and brand:
                    return brand[:60]
    except Exception:
        pass

    return None


def _extract_model(page):
    """
    Best-effort model extraction. Looks for:
      1. "Model:" / "Model Number:" / "Item #:" labels in the specs/details area
      2. JSON-LD `mpn` (manufacturer part number) or `sku`
    Returns string or None.
    """
    try:
        for sel in (
            "div.product-info-description-container",
            "div[class*='product-info']",
            "div[class*='product-details']",
            ".product-info-section",
            "body",
        ):
            container = page.query_selector(sel)
            if not container:
                continue
            text = container.inner_text()
            for label in (r"Model\s*Number", r"Model", r"Item\s*#", r"Item\s*Number", r"MPN"):
                m = re.search(rf"\b{label}\s*[:\-]\s*([\w\-\./]{{2,40}})", text, re.IGNORECASE)
                if m:
                    return m.group(1).strip().strip(",.")
            break
    except Exception:
        pass

    try:
        scripts = page.query_selector_all('script[type="application/ld+json"]')
        for s in scripts:
            try:
                payload = json.loads(s.inner_text() or "{}")
            except Exception:
                continue
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ("mpn", "sku", "productID"):
                    v = item.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()[:40]
    except Exception:
        pass

    return None


def _parse_currency(text: str) -> float | None:
    """Extract first dollar amount from text like '$12.34' or 'Free'."""
    if not text:
        return None
    text = text.strip()
    if text.lower() in ("free", "$0.00", "0.00"):
        return 0.0
    m = re.search(r"\$?\s*([\d,]+\.?\d*)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def get_cart_estimate(costco_url: str, page) -> dict:
    """
    Adds a product to cart, navigates to checkout cart, scrapes shipping estimate,
    then removes the item. Tax is not available pre-checkout on Costco.

    Returns dict with keys: subtotal, shipping (both floats, may be absent).
    Returns {} if product can't be added to cart or any step fails.
    """
    out: dict = {}
    try:
        # Skip re-navigation if already on the product page (called right after scrape_costco)
        current = page.url.split("?")[0].rstrip("/")
        target  = costco_url.split("?")[0].rstrip("/")
        if current != target:
            page.goto(costco_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

        atc_btn = page.query_selector(
            "#add-to-cart-btn, "
            "button[id*='addToCart'], button[id*='add-to-cart'], "
            "button[class*='addToCart'], "
            "[data-automation='product-add-to-cart'], "
            "button:has-text('Add to Cart'), button:has-text('Add to cart')"
        )
        if not atc_btn:
            logger.debug("  cart_estimate: no Add to Cart button — skipping")
            return out

        # Snapshot existing cart items so we only remove what we added
        pre_url = page.url
        atc_btn.click()
        page.wait_for_timeout(2500)

        # If click navigated away (drawer opened wrong product), bail
        if page.url.split("?")[0].rstrip("/") != pre_url.split("?")[0].rstrip("/"):
            logger.debug(f"  cart_estimate: ATC click changed URL — not a direct add, skipping")
            return out

        page.goto("https://www.costco.com/CheckoutCartView",
                  timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Parse order summary using its text content (CSS classes vary by product type)
        summary_el = page.query_selector(
            "[class*='order-summary'], [class*='orderSummary'], "
            "[class*='cart-summary'], [class*='CartSummary']"
        )
        if summary_el:
            summary_text = summary_el.inner_text()
            # "Shipping & Handling for 77433\n$0.00"
            ship_m = re.search(r"Shipping\s*[&]\s*Handling[^\n]*\n\$?([\d,]+\.?\d*)", summary_text)
            if ship_m:
                out["shipping"] = float(ship_m.group(1).replace(",", ""))
            sub_m = re.search(r"Subtotal\n\$?([\d,]+\.?\d*)", summary_text)
            if sub_m:
                out["subtotal"] = float(sub_m.group(1).replace(",", ""))
            # Tax is not available pre-checkout on Costco ("Applicable taxes will be calculated...")
            # Don't populate out["tax"] — researcher falls back to formula =G*0.0825

        logger.info(f"  cart_estimate: {out}")

        # Remove — Costco cart uses "removeItemLink-btn_XXXX" button IDs
        remove_btns = page.query_selector_all("button[id*='removeItemLink']")
        if remove_btns:
            # Click the last one (most recently added item is typically last)
            remove_btns[-1].click()
            page.wait_for_timeout(1500)
            logger.debug("  cart_estimate: item removed from cart")
        else:
            # Fallback text-based remove
            for sel in ("button:has-text('Remove')", "a:has-text('Remove')",
                        "[class*='remove-item']"):
                try:
                    rm = page.query_selector(sel)
                    if rm:
                        rm.click()
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue
            else:
                logger.warning("  cart_estimate: no Remove button found — cart may have a stale item")

    except PlaywrightTimeout:
        logger.warning(f"  cart_estimate: timeout for {costco_url}")
    except Exception as e:
        logger.warning(f"  cart_estimate: failed: {e}")

    return out


def scrape_costco(url, page):
    """
    Scrapes a Costco product URL using the CDP-connected Chrome page.
    Price is captured by intercepting Costco's display-price API call
    (it never appears in the DOM — Costco renders it client-side via a
    separate fetch to gdx-api.costco.com/catalog/product/dispprice-api).

    Returns: {"price": float|None, "stock_status": str, "image_urls": list,
              "title": str|None, "brand": str|None, "model": str|None,
              "item_number": str|None, "purchase_limit": int|None,
              "in_stock": bool, "error": str|None, "http_status": int|None}
    """
    result = {
        "price": None, "stock_status": "Unknown",
        "image_urls": [], "title": None,
        "brand": None, "model": None,
        "item_number": None, "purchase_limit": None, "in_stock": False,
        "error": None, "http_status": None,
    }

    # Item number from URL (most reliable — format: .product.1999611.html)
    m_item = re.search(r"\.product\.(\d+)\.html", url)
    if m_item:
        result["item_number"] = m_item.group(1)

    # Intercept price API before navigation so we catch it on page load
    captured_price = []

    def _on_price_response(response):
        url = response.url
        try:
            if "AjaxGetContractPrice" in url:
                data = response.json()
                price = data.get("finalOnlinePrice")
                if price and float(price) > 0:
                    captured_price.append(float(price))
            elif "dispprice-api" in url:
                data = response.json()
                price = (
                    data.get("priceData", {})
                        .get("displayPrice", {})
                        .get("onlinePrice")
                )
                if price and float(price) > 0:
                    captured_price.append(float(price))
        except Exception:
            pass

    page.on("response", _on_price_response)

    try:
        page.wait_for_timeout(random.randint(800, 2000))

        response = page.goto(
            url, timeout=30000, wait_until="domcontentloaded",
            referer="https://www.costco.com/",
        )
        result["http_status"] = response.status if response else None

        if response and response.status >= 400:
            result["error"] = f"HTTP {response.status}: blocked by server"
            result["stock_status"] = "CHECK FAILED"
            try:
                page.goto("https://www.costco.com", timeout=15000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
            except Exception:
                pass
            return result

        # Give the price API call time to fire (it loads after DOM)
        page.wait_for_timeout(2000 + random.randint(0, 1500))
        page.evaluate("window.scrollTo(0, 300)")
        page.wait_for_timeout(random.randint(400, 900))
        page.evaluate("window.scrollTo(0, 600)")
        page.wait_for_timeout(random.randint(300, 700))

        html = page.content()
        page_lower = html.lower()

        # Akamai embeds "captcha" in its JS on every Costco page — don't use it alone.
        # A real bot block is small (<5KB) or is purely an access-denied page.
        genuinely_blocked = (
            len(html) < 5000 or
            ("access denied" in page_lower and len(html) < 15000) or
            ("captcha" in page_lower and "add to cart" not in page_lower and len(html) < 50000)
        )
        if genuinely_blocked:
            result["error"] = f"Bot/CAPTCHA page ({len(html)} chars)"
            result["stock_status"] = "CHECK FAILED"
            return result

        # Price from intercepted API call
        if captured_price:
            result["price"] = captured_price[0]
            logger.debug(f"  Price from API: ${captured_price[0]}")

        # ── Stock detection ───────────────────────────────────────────────────
        # Priority 1: "Add to Cart" button present = definitely In Stock.
        # This beats any text match because the button is only rendered when
        # the product is purchasable online.
        add_to_cart = page.query_selector(
            "#add-to-cart-btn, "
            "button[id*='addToCart'], button[id*='add-to-cart'], "
            "button[class*='addToCart'], "
            "[data-automation='product-add-to-cart'], "
            "button:has-text('Add to Cart'), button:has-text('Add to cart')"
        )

        # Product info container has the real rendered text (limit, brand, specs).
        # page.inner_text("body") is unreliable for JS-heavy pages — grab the container.
        prod_text = ""
        for sel in ("[class*='product-info']", "[class*='product-detail']",
                    ".product-info-section", "body"):
            try:
                el = page.query_selector(sel)
                if el:
                    t = el.inner_text()
                    if len(t) > 200:
                        prod_text = t.lower()
                        break
            except Exception:
                continue

        # Purchase limit — Costco formats vary:
        #   "Limit of 1 Transaction Per Membership, with a Maximum of 4 Units Per 24 Hours"
        #   "Limit 5 per member"
        for lim_rx in (
            r"maximum\s+of\s+(\d+)\s+units",
            r"limit\s+of\s+(\d+)\s+transaction",
            r"limit\s+(\d+)\s+per\s+(?:member|household|order|customer)",
            r"max(?:imum)?\s+(\d+)\s+per",
        ):
            lim_m = re.search(lim_rx, prod_text)
            if lim_m:
                result["purchase_limit"] = int(lim_m.group(1))
                break

        body_text = prod_text  # reuse for stock checks below

        if add_to_cart:
            atc_class = (add_to_cart.get_attribute("class") or "").lower()
            if "out-of-stock" in atc_class or "out_of_stock" in atc_class:
                # Button present but disabled — item is currently sold out
                result["stock_status"] = "OUT OF STOCK"
            elif any(p in body_text for p in ["while supplies last", "limited quantity", "low stock"]):
                result["stock_status"] = "Limited"
                result["in_stock"] = True
            elif result["purchase_limit"]:
                result["stock_status"] = "Limited"
                result["in_stock"] = True
            else:
                result["stock_status"] = "In Stock"
                result["in_stock"] = True
        else:
            # No Add to Cart button — fall back to visible body text scan.
            # We use inner_text (rendered text only) rather than raw HTML to
            # avoid false positives from <script> JSON-LD or carousel widgets.
            if any(p in body_text for p in ["available in club only", "warehouse only",
                                             "not available online", "in-store only", "club only"]):
                result["stock_status"] = "WAREHOUSE ONLY"
            elif any(p in body_text for p in ["out of stock", "currently unavailable", "sold out"]):
                result["stock_status"] = "OUT OF STOCK"
            elif any(p in body_text for p in ["limited", "while supplies last", "low stock"]):
                result["stock_status"] = "Limited"
                result["in_stock"] = True
            else:
                # No Add to Cart button and no explicit OOS text — flag for review
                result["stock_status"] = "Unknown"

        # Costco product images are served from two CDNs:
        #   richmedia.channeladvisor.com  — main product photos
        #   images.costco-static.com      — fallback / alternate images
        # Try CDN-matched srcs first, then fall back to container selectors.
        def _collect_img_urls(els, limit=5):
            seen, urls = set(), []
            for img in els:
                src = img.get_attribute("src") or img.get_attribute("data-src") or ""
                src = src.strip()
                if not src or src in seen:
                    continue
                skip = any(x in src.lower() for x in ["logo", "icon", "banner", "sprite", "svg"])
                if skip:
                    continue
                seen.add(src)
                urls.append(src)
                if len(urls) >= limit:
                    break
            return urls

        cdn_imgs = page.query_selector_all(
            "img[src*='channeladvisor'], img[src*='costco-static'], "
            "img[data-src*='channeladvisor'], img[data-src*='costco-static']"
        )
        urls = _collect_img_urls(cdn_imgs)

        if not urls:
            container_imgs = page.query_selector_all(
                "[class*='product-image'] img, [class*='image-viewer'] img, "
                "[class*='carousel'] img, [class*='thumbnail'] img, "
                ".product-info-section img"
            )
            urls = _collect_img_urls(container_imgs)

        result["image_urls"] = urls

        h1 = page.query_selector("h1")
        if h1:
            result["title"] = h1.inner_text().strip()

        # Brand + model — best-effort, multiple fallbacks. Either may stay None.
        result["brand"] = _extract_brand(page)
        result["model"] = _extract_model(page)
        if result["brand"] or result["model"]:
            logger.debug(f"  Brand={result['brand']!r} Model={result['model']!r}")

        # Weight + karat — for precious metals margin calc
        from tools.spot_price import parse_gold_weight
        result["weight_oz"], result["karat"] = parse_gold_weight(result.get("title") or "")

    except PlaywrightTimeout:
        result["error"] = "Timed out after 30s"
        result["stock_status"] = "CHECK FAILED"
    except Exception as e:
        result["error"] = str(e)
        result["stock_status"] = "CHECK FAILED"
    finally:
        page.remove_listener("response", _on_price_response)

    return result
