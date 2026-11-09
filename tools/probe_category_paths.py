"""
Debug tool: dump the Costco category paths behind savings search results.

Crawls a search listing (default the savings "OFF" listing) with the same crawler as --mode savings,
records every product's raw `product.categories` (all cumulative paths, not just the deepest one that
parse_search_result keeps), and prints the unique paths that look like jewelry / watches / gold, grouped
by top-level department, with which path parse_search_result would pick and how business.savings.paths
classifies it. Run by hand before editing business.savings.paths.

    python tools/probe_category_paths.py [--keyword OFF] [--keyword diamond ...] [--pages N] [--force]

Saves everything to .tmp/category_probe/paths.json. Refuses to run while a scheduler run holds
data/.scheduler_lock (--force overrides).
"""
import json
import os
import re
import sys
from collections import defaultdict
from urllib.parse import quote_plus

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.costco_savings import SEARCH_API, compile_keywords, classify_category, parse_search_result  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, ".tmp", "category_probe")
INTEREST_RX = re.compile(r"watch|jewel|diamond|gold|ring|earring|necklace|bracelet|pendant", re.IGNORECASE)


def _args(argv):
    keywords, pages = [], 110
    it = iter(argv)
    for a in it:
        if a == "--keyword":
            keywords.append(next(it, "OFF"))
        elif a == "--pages":
            pages = int(next(it, "110"))
    return keywords or ["OFF"], pages


def _path_rules():
    import yaml
    with open(os.path.join(ROOT, "config", "categories.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return compile_keywords(((cfg.get("business") or {}).get("savings") or {}).get("paths"))


def main(argv) -> None:
    from tools.costco_savings import scrape_search_listing
    from tools.costco_scraper import make_browser, refresh_session

    keywords, pages = _args(argv)
    os.makedirs(OUT, exist_ok=True)
    products = {}                                  # id -> {title, categories, chosen, keyword}

    with make_browser() as page:
        current = {"kw": None}

        def on_response(resp):
            if SEARCH_API not in resp.url or resp.request.method != "POST":
                return
            try:
                results = (resp.json().get("searchResult") or {}).get("results") or []
            except Exception:
                return
            for r in results:
                parsed = parse_search_result(r)
                if not parsed:
                    continue
                cats = [c for c in ((r.get("product") or {}).get("categories") or []) if isinstance(c, str)]
                products[parsed["product_id"]] = {"title": parsed["title"], "categories": cats,
                                                  "chosen": parsed["category_path"], "keyword": current["kw"]}

        page.on("response", on_response)
        for kw in keywords:
            current["kw"] = kw
            url = f"https://www.costco.com/s?keyword={quote_plus(kw)}&dept=All"
            items, meta = scrape_search_listing(page, url, max_pages=pages, refresh=refresh_session)
            print(f"[{kw}] {len(items)} items, meta {meta}")

    rules = _path_rules()
    by_path = defaultdict(list)
    for pid, p in products.items():
        for c in p["categories"]:
            by_path[c].append(pid)
    with open(os.path.join(OUT, "paths.json"), "w", encoding="utf-8") as f:
        json.dump({"products": products, "paths": {k: len(v) for k, v in by_path.items()}}, f, indent=1)

    print(f"\n{len(products)} products, {len(by_path)} unique paths\n")
    print("=== Unique paths matching watch|jewel|diamond|gold|ring|earring|necklace|bracelet|pendant ===")
    groups = defaultdict(list)
    for path in sorted(by_path):
        if INTEREST_RX.search(path):
            groups[path.split(">")[0].strip()].append(path)
    for top in sorted(groups):
        print(f"\n## {top}")
        for path in groups[top]:
            print(f"  {len(by_path[path]):4d}  {path}   -> {classify_category(path, rules)}")

    print("\n=== Products whose categories match (chosen path = what --mode savings classifies) ===")
    for pid, p in sorted(products.items(), key=lambda kv: kv[1]["chosen"]):
        if any(INTEREST_RX.search(c) for c in p["categories"]) or INTEREST_RX.search(p["title"]):
            print(f"- {p['title'][:70]}  [{pid}]\n    chosen: {p['chosen']}  -> {classify_category(p['chosen'], rules)}")
            for c in p["categories"]:
                if c != p["chosen"]:
                    print(f"    also:   {c}")
    print(f"\nSaved to {OUT}")


if __name__ == "__main__":
    from tools.probe_guard import refuse_if_scheduler_busy
    refuse_if_scheduler_busy()
    main(sys.argv[1:])
