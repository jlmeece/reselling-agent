"""
Debug tool: dump Costco's inventory-availability API responses for product pages.

The "How to get it" section (#fulfillment-title) is filled client-side from
ecom-api.costco.com/ebusiness/inventory/v1/inventorylevels/availability/{batch,v2,pickup}.
For each URL this attaches its own response listener, runs the normal scrape_costco()
(unchanged), and saves into .tmp/inventory_probe/:
  <item>_<n>.json    every inventorylevels/availability/* and AjaxSCInventoryUpdate response
                     (method, URL, request body, status, JSON body)
  <item>_ui.txt      the rendered "How to get it" text — the ground truth to compare against
Run by hand when Costco changes the fulfillment widget; the parser in
tools/costco_scraper.py (_parse_inventory_payload) follows what this prints.

    python tools/probe_inventory.py <url> [<url> ...]
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "inventory_probe")

_UI_JS = """() => {
  const t = document.querySelector('#fulfillment-title');
  if (!t) return null;
  let el = t;
  for (let i = 0; i < 4 && el.parentElement; i++) {
    el = el.parentElement;
    if ((el.innerText || '').length > 120) break;
  }
  return el.innerText;
}"""


def probe(page, url):
    from tools.costco_scraper import scrape_costco

    item = (re.search(r"\.product\.(\d+)\.html", url) or re.search(r"(\d{6,})", url))
    item = item.group(1) if item else "item"
    hits = []

    def on_response(resp):
        u = resp.url
        if not any(k in u for k in ("inventorylevels", "AjaxSCInventoryUpdate")):
            return
        try:
            body = resp.json()
        except Exception:
            body = None
        try:
            req_body = resp.request.post_data
        except Exception:
            req_body = None
        hits.append({"method": resp.request.method, "url": u, "status": resp.status,
                     "request_body": req_body, "json": body})

    page.on("response", on_response)
    try:
        result = scrape_costco(url, page)
        page.wait_for_timeout(1500)            # late inventory calls
        ui = page.evaluate(_UI_JS)
    finally:
        page.remove_listener("response", on_response)

    print(f"\n=== {item}  {url}")
    print(f"scrape_costco: stock_status={result['stock_status']!r} in_stock={result['in_stock']} "
          f"price={result['price']} error={result['error']}")
    print(f"               delivery={result.get('delivery_status')!r} "
          f"pickup={result.get('pickup_status')!r} source={result.get('stock_source')!r}")
    print(f"How to get it (UI): {(ui or '(not found)')!r}")
    with open(os.path.join(OUT, f"{item}_ui.txt"), "w", encoding="utf-8") as f:
        f.write(ui or "")
    for i, h in enumerate(hits):
        path = os.path.join(OUT, f"{item}_{i}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(h, f, indent=1)
        print(f"[{i}] {h['method']} {h['status']} {h['url'][:160]}")
        print(f"     -> {path}")
    if not hits:
        print("NO inventory/availability responses captured")
    return result, ui, hits


def main(urls):
    from tools.costco_scraper import make_browser
    os.makedirs(OUT, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    with make_browser() as page:
        for url in urls:
            probe(page, url)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
