"""
Quick smoke test — run this on the VPS to verify Chrome CDP works on Linux.
Usage: venv/bin/python tools/test_chrome_linux.py
"""
import sys
print(f"Platform: {sys.platform}")

from tools.costco_scraper import _ensure_chrome, _debug_port_open, _kill_agent_chrome

print("Launching Chrome...")
_ensure_chrome()
print(f"Debug port open: {_debug_port_open()}")

from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
browser = pw.chromium.connect_over_cdp("http://localhost:9222", timeout=30000)
page = browser.contexts[0].new_page() if browser.contexts else browser.new_context().new_page()
page.goto("https://www.google.com", timeout=20000, wait_until="domcontentloaded")
print(f"Page title: {page.title()}")
page.close()
browser.close()
pw.stop()
_kill_agent_chrome()
print("Done — Chrome CDP works on Linux.")
