"""
Export Costco cookies from the agent Chrome profile into costco_cookies.json.

The agent launches its own Chrome window (CostcoAgentProfile). Log into
costco.com in that window, then run this script — it reads the cookies
directly from the live browser via CDP. No extension needed.

USAGE:
  1. Run the agent once so Chrome opens (or start it manually via the researcher).
  2. In the agent Chrome window, go to costco.com and log in.
  3. Run: python tools/setup_costco_session.py

RENEWAL:
  Re-run whenever the scraper starts returning CAPTCHA/CHECK FAILED (~30 days).
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
from tools.costco_scraper import _ensure_chrome, CHROME_DEBUG_URL

COOKIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "costco_cookies.json"
)


def setup_session():
    print("Connecting to agent Chrome to export Costco cookies...")
    _ensure_chrome()

    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(CHROME_DEBUG_URL)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # Navigate to Costco so all cookies are loaded
        page = context.new_page()
        try:
            print("Loading costco.com to ensure all cookies are present...")
            page.goto("https://www.costco.com", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        finally:
            page.close()

        all_cookies = context.cookies()
        costco_cookies = [c for c in all_cookies if "costco" in c.get("domain", "").lower()]

        if not costco_cookies:
            print("ERROR: No costco.com cookies found.")
            print("Make sure you're logged into costco.com in the agent Chrome window, then try again.")
            return

        # Check for Akamai bot token
        akamai = [c for c in costco_cookies if c.get("name", "").startswith("bm_")]
        logged_in = any(c.get("name") in ("ak_bmsc", "bm_sv", "costco_member") or
                        "auth" in c.get("name", "").lower()
                        for c in costco_cookies)

        # Convert Playwright cookie format to Cookie-Editor format for compatibility
        out = []
        for c in costco_cookies:
            entry = {
                "name":     c["name"],
                "value":    c["value"],
                "domain":   c["domain"],
                "path":     c.get("path", "/"),
                "secure":   c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": c.get("sameSite", "Lax"),
            }
            if c.get("expires") and c["expires"] > 0:
                entry["expirationDate"] = c["expires"]
            out.append(entry)

        with open(COOKIES_PATH, "w") as f:
            json.dump(out, f, indent=2)

        print(f"\nSaved {len(out)} Costco cookies to {COOKIES_PATH}")
        print(f"  Akamai bot tokens: {len(akamai)}")
        print(f"  Appears logged in: {'yes' if logged_in else 'uncertain — verify in Chrome'}")
        print("\nRun the agent: python agents/researcher.py")

    finally:
        pw.stop()


if __name__ == "__main__":
    setup_session()
