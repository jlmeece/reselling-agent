"""
Debug tool: dump the structure of Costco's Member-Only Savings page.

Logs every gdx-api.costco.com response (URL + JSON top-level keys), saves the raw
JSON of any search/listing-shaped response, the rendered card markup, and the
category tab / "load more" controls into .tmp/savings_probe/. Run once by hand
when Costco redesigns the page; the parser in tools/costco_savings.py follows it.

    python tools/probe_savings_page.py [url]
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.costco_savings import SAVINGS_URL  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "savings_probe")


def main(url: str) -> None:
    from tools.costco_scraper import make_browser

    os.makedirs(OUT, exist_ok=True)
    api = []

    def on_response(resp):
        if "costco.com" in resp.url and ("gdx-api" in resp.url or "/api/" in resp.url):
            try:
                body = resp.json()
            except Exception:
                return
            keys = list(body.keys())[:12] if isinstance(body, dict) else type(body).__name__
            api.append((resp.url, keys, body))

    with make_browser() as page:
        page.on("response", on_response)
        resp = page.goto(url, timeout=45000, wait_until="domcontentloaded", referer="https://www.costco.com/")
        print(f"HTTP {resp.status if resp else '?'}  {page.url}")
        page.wait_for_timeout(6000)
        for pos in (800, 2000, 4000, 8000, 12000):
            page.evaluate(f"window.scrollTo(0, {pos})")
            page.wait_for_timeout(800)

        for i, (u, keys, body) in enumerate(api):
            print(f"[api {i}] {u[:150]}\n         keys={keys}")
            with open(os.path.join(OUT, f"api_{i}.json"), "w", encoding="utf-8") as f:
                json.dump(body, f)

        info = page.evaluate(
            """() => {
              const out = {};
              out.title = document.title;
              out.banner = (document.body.innerText.match(/Valid\\s+\\d+\\/\\d+\\/\\d+\\s*-\\s*\\d+\\/\\d+\\/\\d+/) || [null])[0];
              out.productLinks = document.querySelectorAll("a[href*='.product.']").length;
              out.tabs = [...document.querySelectorAll("[role=tab], [role=tablist] a, [role=tablist] button")]
                          .slice(0, 40).map(e => (e.innerText || '').trim()).filter(Boolean);
              out.moreBtns = [...document.querySelectorAll("button, a")]
                          .filter(e => /load more|show more|view more|see all/i.test(e.innerText || ''))
                          .slice(0, 10).map(e => (e.innerText || '').trim());
              const a = document.querySelector("a[href*='.product.']");
              let card = a;
              for (let i = 0; a && i < 6 && card.parentElement; i++) {
                card = card.parentElement;
                if ((card.innerText || '').length > 60) break;
              }
              out.sampleCards = [...document.querySelectorAll("a[href*='.product.']")].slice(0, 3).map(l => {
                let c = l;
                for (let i = 0; i < 6 && c.parentElement; i++) { c = c.parentElement; if ((c.innerText||'').length > 60) break; }
                return {href: l.getAttribute('href'), text: (c.innerText || '').slice(0, 400), html: c.outerHTML.slice(0, 1500)};
              });
              return out;
            }"""
        )
        with open(os.path.join(OUT, "dom.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
        with open(os.path.join(OUT, "page.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
        print(json.dumps({k: v for k, v in info.items() if k != "sampleCards"}, indent=2))
        for c in info.get("sampleCards", []):
            print("--- card:", c["href"], "\n", re.sub(r"\s+\n", "\n", c["text"]))
    print(f"\nSaved to {OUT}")


def probe_search(max_pages: int = 70) -> None:
    """Crawl the full 'OFF' search listing and dump every parsed item + a category-path breakdown to
    .tmp/savings_probe/search_items.json (used to design business.savings.paths)."""
    from collections import Counter
    from tools.costco_savings import SEARCH_URL, scrape_search_listing
    from tools.costco_scraper import make_browser, refresh_session

    os.makedirs(OUT, exist_ok=True)
    with make_browser() as page:
        items, meta = scrape_search_listing(page, SEARCH_URL, max_pages=max_pages, refresh=refresh_session)
    print(meta)
    with open(os.path.join(OUT, "search_items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)
    top = Counter(i["section"] for i in items)
    print(f"{len(items)} items; distinct ids {len({i['product_id'] for i in items})}")
    print("top-level:", top.most_common(30))
    lvl3 = Counter(" > ".join(i["category_path"].split(" > ")[:3]) for i in items)
    for path, n in sorted(lvl3.items()):
        print(f"{n:4d}  {path}")


if __name__ == "__main__":
    if "--search" in sys.argv:
        probe_search()
    else:
        main(next((a for a in sys.argv[1:] if not a.startswith("--")), SAVINGS_URL))
