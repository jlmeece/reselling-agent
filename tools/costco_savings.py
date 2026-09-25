"""
Tool: costco_savings
Scrape Costco's Member-Only Savings page (warehouse-savings) and cross-reference the items
against the Product Tracker. Everything except `scrape_savings_listing` is pure (no browser /
sheet / Telegram) so it is unit-testable.

Page structure (probed 2026-09-24 with tools/probe_savings_page.py):
  * https://www.costco.com/warehouse-savings.html redirects to /o/-/warehouse-savings.
  * Server/JS-rendered DOM, NOT an API listing: no catalog/search API call carries the items.
  * One <h2> per department (Apparel, Appliances, Grocery, Health & Personal Care, Pharmacy ...).
  * A card is the innermost [data-testid=Grid] holding exactly one product link
    (`/.product.<id>.html`, occasionally `/p/-/<slug>/<id>`). ~185 distinct items per event.
  * Card text: title, "Warehouse & Online" | "Online Only" | "Warehouse Only", "Item <warehouse
    item no>", "Limit N", then EITHER "$ 15 . 99 After $4 OFF" (sale price + savings) OR just
    "Save $ 3" (savings only). Some cards (TVs, deli) carry no savings text at all.
  * Banner: "Valid 9/21/26 - 10/18/26" (the whole event window, not per item).
  * Brand-event tiles link to category/keyword URLs, not product pages — ignored.
Listing text is only a HINT: authoritative price/regular/end come from the product page's price
API (scrape_costco), which the caller runs for each candidate.
"""

import html
import re

from loguru import logger

from tools.ebay_sync import _norm_title, compute_net, token_jaccard, CLOSE_MATCH_JACCARD
from tools.sale_monitor import parse_rate, to_float

SAVINGS_URL = "https://www.costco.com/warehouse-savings.html"

_PRODUCT_RX = re.compile(r"\.product\.(\d+)\.html|/p/-/[^/?#]+/(\d+)")
_MONEY = r"(\d[\d,]*(?:\.\d+)?)"
_AFTER_RX = re.compile(r"\$" + _MONEY + r"\s*After\s*\$" + _MONEY + r"\s*OFF", re.IGNORECASE)
_SAVE_RX = re.compile(r"Save\s*\$" + _MONEY, re.IGNORECASE)
_ITEM_RX = re.compile(r"\bItem\s+(\d+)")
_LIMIT_RX = re.compile(r"\bLimit\s+(\d+)")
_BANNER_RX = re.compile(r"Valid\s+\d{1,2}/\d{1,2}/\d{2,4}\s*-\s*(\d{1,2}/\d{1,2}/\d{2,4})")

# Runs in the page: one dict per distinct product (the richest card wins when a product renders twice).
_EXTRACT_JS = r"""
() => {
  const rx = /\.product\.(\d+)\.html|\/p\/-\/[^\/?#]+\/(\d+)/;
  const idOf = h => { const m = rx.exec(h || ''); return m ? (m[1] || m[2]) : null; };
  const h2s = [...document.querySelectorAll('h2')];
  const sectionOf = el => {
    let s = '';
    for (const h of h2s) {
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) s = (h.innerText || '').trim();
      else break;
    }
    return s;
  };
  const best = new Map();
  for (const g of document.querySelectorAll('[data-testid=Grid]')) {
    const links = [...g.querySelectorAll('a[href]')].filter(a => idOf(a.href));
    const ids = new Set(links.map(a => idOf(a.href)));
    if (ids.size !== 1) continue;
    const id = [...ids][0];
    const text = (g.innerText || '').trim();
    const nPrices = g.querySelectorAll('[data-testid=Text_prices_and_percentages_prices]').length;
    const score = nPrices * 10000 + text.length;
    const title = links.map(a => (a.innerText || '').trim()).find(t => t)
                  || ((g.querySelector('img') || {}).alt || '');
    if (!best.has(id) || score > best.get(id).score)
      best.set(id, {href: links[0].href, title, text, section: sectionOf(g), score});
  }
  return {
    cards: [...best.values()],
    linkIds: new Set([...document.querySelectorAll('a[href]')].map(a => idOf(a.href)).filter(Boolean)).size,
    body: (document.body.innerText || '').slice(0, 40000),
  };
}
"""


# ── pure helpers ─────────────────────────────────────────────────────────────

def product_id(url):
    """Costco product id from '/.product.4000422365.html' or '/p/-/slug/100217166'; None otherwise."""
    m = _PRODUCT_RX.search(str(url or ""))
    return (m.group(1) or m.group(2)) if m else None


def norm_url(url) -> str:
    """URL without query string / fragment / trailing slash, lowercased (sheet col R vs page hrefs)."""
    return str(url or "").split("?")[0].split("#")[0].rstrip("/").lower()


def _money(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _squash(text) -> str:
    """innerText of a price is split across nodes ('$ 15 . 99'); rejoin -> '$15.99'."""
    t = " ".join(str(text or "").split())
    t = re.sub(r"\$\s*(\d[\d,]*)\s*\.\s*(\d{1,2})\b", r"$\1.\2", t)
    return re.sub(r"\$\s+(?=\d)", "$", t)


def parse_card(raw) -> dict:
    """
    raw {href, title, text, section} (from the page) -> item dict:
    {title, url, product_id, section, item_number, limit, tag, list_sale, list_savings, list_regular}
    Shapes: "$15.99 After $4 OFF" -> sale 15.99, savings 4, regular 19.99;  "Save $3" -> savings 3 only.
    """
    text = _squash(raw.get("text"))
    sale = savings = regular = None
    m = _AFTER_RX.search(text)
    if m:
        sale, savings = _money(m.group(1)), _money(m.group(2))
        if sale is not None and savings is not None:
            regular = round(sale + savings, 2)
    else:
        m = _SAVE_RX.search(text)
        if m:
            savings = _money(m.group(1))
    tag = ""
    for label in ("Warehouse & Online", "Online Only", "Warehouse Only"):
        if label.lower() in text.lower():
            tag = label
            break
    item = _ITEM_RX.search(text)
    limit = _LIMIT_RX.search(text)
    href = str(raw.get("href") or "")
    return {
        "title": " ".join(str(raw.get("title") or "").split()),
        "url": href.split("?")[0],
        "product_id": product_id(href),
        "section": str(raw.get("section") or ""),
        "item_number": item.group(1) if item else "",
        "limit": int(limit.group(1)) if limit else None,
        "tag": tag,
        "list_sale": sale,
        "list_savings": savings,
        "list_regular": regular,
        "source": "page",
    }


def page_end_date(text):
    """'Valid 9/21/26 - 10/18/26' banner -> '10/18/26' (end of the whole event); None if absent."""
    m = _BANNER_RX.search(str(text or ""))
    return m.group(1) if m else None


def savings_rank(item) -> tuple:
    """Ranking key for new-item candidates, best first when sorted descending: (savings / regular,
    savings $). "Save $N"-only cards have no regular price, so they fall back to the $ amount."""
    sav, reg = item.get("list_savings") or 0.0, item.get("list_regular")
    return (sav / reg if sav and reg else 0.0, sav)


# ── category classification (for NEW rows) ───────────────────────────────────

def compile_keywords(keyword_cfg) -> list:
    """
    {category: ["regex fragment", "!excluded fragment", ...]} -> [(category, include_rx, exclude_rx|None)]
    in config order (first category wins). Fragments are regex, wrapped in word boundaries.
    """
    out = []
    for category, entries in (keyword_cfg or {}).items():
        inc = [e for e in (entries or []) if not str(e).startswith("!")]
        exc = [str(e)[1:] for e in (entries or []) if str(e).startswith("!")]
        if not inc:
            continue
        inc_rx = re.compile(r"\b(?:" + "|".join(inc) + r")\b", re.IGNORECASE)
        exc_rx = re.compile(r"\b(?:" + "|".join(exc) + r")\b", re.IGNORECASE) if exc else None
        out.append((category, inc_rx, exc_rx))
    return out


def classify_category(title, compiled):
    """First category whose include pattern hits and whose exclude pattern doesn't; else None."""
    t = str(title or "")
    for category, inc_rx, exc_rx in compiled:
        if inc_rx.search(t) and not (exc_rx and exc_rx.search(t)):
            return category
    return None


# ── sheet side + cross-reference ─────────────────────────────────────────────

def _idx(letter):
    n = 0
    for c in letter.upper():
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


def load_rows(raw_rows, COL, start_row=4) -> list:
    """Product Tracker rows (A:AW) -> dicts for matching/alerting. Skips rows with no title."""
    keys = ("status", "title", "category", "costco_url", "costco_cost", "sale_info", "regular_price",
            "ebay_price", "fee_rate", "ship_cost")
    idx = {k: _idx(COL[k]) for k in keys if k in COL}
    rows = []
    for offset, r in enumerate(raw_rows):
        cells = {k: (str(r[i]).strip() if i < len(r) else "") for k, i in idx.items()}
        if not cells.get("title"):
            continue
        cells["row_num"] = start_row + offset
        cells["product_id"] = product_id(cells.get("costco_url"))
        cells["norm_url"] = norm_url(cells.get("costco_url"))
        cells["norm_title"] = _norm_title(cells["title"])
        rows.append(cells)
    return rows


def match_items(items, rows) -> dict:
    """
    Cross-reference savings-page items against sheet rows. Returns
    {"tracked": [(item, row, how)], "possible": [(item, row)], "ambiguous": [item], "new": [item]}
      tracked   product id in col R (any slug) | same normalised URL | identical normalised title
      possible  token-Jaccard >= CLOSE_MATCH_JACCARD, unique best row — reported, never written and
                never added as new (it is almost certainly a row we already have)
      ambiguous best score shared by several rows, or one row wanted by several items — same treatment
      new       nothing close: a genuinely untracked item
    """
    by_pid, by_url, by_title = {}, {}, {}
    for r in rows:
        if r["product_id"]:
            by_pid.setdefault(r["product_id"], r)
        if r["norm_url"]:
            by_url.setdefault(r["norm_url"], r)
        if r["norm_title"]:
            by_title.setdefault(r["norm_title"], r)

    result = {"tracked": [], "possible": [], "ambiguous": [], "new": []}
    seen_pid = set()
    close = []                                            # (item, [rows tied for best])
    for item in items:
        pid = item.get("product_id")
        if pid:
            if pid in seen_pid:
                continue                                  # same product rendered twice on the page
            seen_pid.add(pid)
        row, how = None, ""
        if pid and pid in by_pid:
            row, how = by_pid[pid], "product_id"
        elif norm_url(item["url"]) in by_url:
            row, how = by_url[norm_url(item["url"])], "url"
        elif _norm_title(item["title"]) in by_title:
            row, how = by_title[_norm_title(item["title"])], "title"
        if row:
            result["tracked"].append((item, row, how))
            continue

        norm = _norm_title(item["title"])
        best_score, best = 0.0, []
        for r in rows:
            if not r["norm_title"] or not norm:
                continue
            jac = token_jaccard(norm, r["norm_title"])
            if jac < CLOSE_MATCH_JACCARD:
                continue
            if jac > best_score:
                best_score, best = jac, [r]
            elif jac == best_score:
                best.append(r)
        if best:
            close.append((item, best))
        else:
            result["new"].append(item)

    wanted = {}
    for item, best in close:
        if len(best) == 1:
            wanted.setdefault(best[0]["row_num"], []).append(item)
    for item, best in close:
        if len(best) == 1 and len(wanted[best[0]["row_num"]]) == 1:
            result["possible"].append((item, best[0]))
        else:
            result["ambiguous"].append(item)
    return result


# ── alert formatting ─────────────────────────────────────────────────────────

def _short_end(end):
    """'10/18/26' -> '10/18' (alerts read better without the year)."""
    m = re.match(r"^(\d{1,2}/\d{1,2})(?:/\d{2,4})?$", str(end or "").strip())
    return m.group(1) if m else ""


def net_profit(row, price):
    """Net at the row's eBay price (col H) for a Costco cost of `price`; None when unknowable."""
    ebay = to_float(row.get("ebay_price")) if row else None
    fee = parse_rate(row.get("fee_rate")) if row else None
    if not ebay or ebay <= 0 or fee is None or not price:
        return None
    ship = to_float(row.get("ship_cost")) or 0.0
    return round(compute_net(ebay, price, fee, ship), 2)


def alert_line(entry) -> str:
    """
    entry {title, price, regular, end, net, new_row} -> one HTML line:
    '🔔 Title on sale — $31.99 (was $39.99, ends 10/18) · net +$7.20'   ('🆕' + ' — added PENDING' for new rows)
    """
    title = html.escape(str(entry["title"])[:60].rstrip(" ,-"), quote=False)   # Telegram HTML: only & < > matter
    detail = []
    if entry.get("regular"):
        detail.append(f"was ${entry['regular']:.2f}")
    end = _short_end(entry.get("end"))
    if end:
        detail.append(f"ends {end}")
    line = f"{'🆕' if entry.get('new_row') else '🔔'} {title} on sale — ${entry['price']:.2f}"
    if detail:
        line += f" ({', '.join(detail)})"
    if entry.get("net") is not None:
        line += f" · net {'+' if entry['net'] >= 0 else '-'}${abs(entry['net']):.2f}"
    if entry.get("new_row"):
        line += " — added PENDING"
    return line


def format_alert(entries, top=15):
    """ONE message per run for every new sale (None when there is nothing to say)."""
    if not entries:
        return None
    lines = [alert_line(e) for e in entries[:top]]
    if len(entries) > top:
        lines.append(f"…and {len(entries) - top} more")
    return "\n".join(lines)


# ── browser side ─────────────────────────────────────────────────────────────

def scrape_savings_listing(page, url=SAVINGS_URL):
    """
    Load the savings page, scroll until the card count stops growing, extract the cards.
    Returns (items, banner_end): items = distinct parse_card() dicts with a title; banner_end =
    '10/18/26' or None. [] items means the layout changed (or the page was blocked) — the caller
    treats that as an error. Never raises.
    """
    try:
        resp = page.goto(url, timeout=45000, wait_until="domcontentloaded",
                         referer="https://www.costco.com/")
        if resp is not None and resp.status >= 400:
            logger.warning(f"  savings page blocked (HTTP {resp.status})")
            return [], None
        page.wait_for_timeout(5000)
        last = -1
        for _ in range(14):                               # bounded: lazy sections load on scroll
            page.evaluate("window.scrollBy(0, 2500)")
            page.wait_for_timeout(700)
            n = page.evaluate("document.querySelectorAll('a[href*=\".product.\"]').length")
            if n == last:
                break
            last = n
        page.evaluate("window.scrollTo(0, 0)")
        data = page.evaluate(_EXTRACT_JS)
    except Exception as e:
        logger.warning(f"  savings page scrape failed: {e}")
        return [], None

    items = [it for it in (parse_card(c) for c in data.get("cards", [])) if it["title"] and it["url"]]
    logger.info(f"  savings page: {len(items)} cards parsed ({data.get('linkIds', 0)} distinct product links on page)")
    return items, page_end_date(data.get("body"))


# ══ Source 2: the full "OFF" search listing ═══════════════════════════════════════════════════
#
# https://www.costco.com/s?keyword=OFF&dept=All  ->  ~1,586 items (`attributes.has_discount` is 1 on
# 1,585 of them, so the keyword search IS the sale catalogue), 24 per page, ~67 pages.
# Probed 2026-09-24:
#   * The SPA POSTs gdx-api.costco.com/catalog/search/api/v1/search with
#     {"query":"OFF","pageSize":24,"offset":(N-1)*24,"filterBy":["HIDE_OUT_OF_STOCK"],...};
#     `&currentPage=N` on the page URL drives it (`&page=N` is ignored). Response: searchResult
#     {results[24], totalSize, nextPageToken, facets}.
#   * Per result: `id` (= the .product.<id>.html id), product.title / uri (`/p/-/<slug>/<id>`),
#     product.categories (cumulative paths, e.g. "Appliances > Small Kitchen Appliances > Air Fryers"),
#     product.attributes.promotional_statement.text ("$40 manufacturer's savings is valid 9/1/26 through
#     9/30/26 ..."), and variantRollupValues with the store-847 hint prices:
#     "inventory(847, price)" (sale), "inventory(847, originalPrice)" (regular),
#     "inventory(847, attributes.promotion_short_text)" ("$40 OFF" / "$50 OFF,$650 OFF").
# The hint prices matched the product-page price API on every item checked, but the price API stays
# the authority: they only pre-filter which items are worth a product-page scrape.

SEARCH_URL = "https://www.costco.com/s?keyword=OFF&dept=All"
SEARCH_API = "gdx-api.costco.com/catalog/search/api/v1/search"
SEARCH_PAGE_SIZE = 24

_PROMO_VALID_RX = re.compile(r"valid\s+\d{1,2}/\d{1,2}/\d{2,4}\s+through\s+(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)
_TAG_RX = re.compile(r"<[^>]+>")


def _first(values):
    """variantRollupValues entries are lists ([159.99]); first usable number or None."""
    for v in (values if isinstance(values, list) else [values]):
        f = to_float(v)
        if f is not None and f > 0:
            return f
    return None


def canonical_url(pid) -> str:
    """The URL form scrape_costco and the sheet already use: works for any product id, unlike the
    search result's /p/-/slug/id form."""
    return f"https://www.costco.com/.product.{pid}.html"


def promo_end(statement):
    """Earliest 'valid M/D/YY through M/D/YY' end date in a promotional statement, else None. Only a
    fallback for when the price API carries no promotionEndDate."""
    best, best_key = None, None
    for e in _PROMO_VALID_RX.findall(str(statement or "")):
        m, d, y = (int(x) for x in e.split("/"))
        key = (y + 2000 if y < 100 else y, m, d)
        if best_key is None or key < best_key:
            best, best_key = e, key
    return best


def parse_search_result(res):
    """One searchResult.results[] entry -> item dict (same shape as parse_card plus category_path,
    promo_short, promo_text, promo_end, source), or None when it has no id/title."""
    p = res.get("product") or {}
    pid = str(res.get("id") or "").strip()
    title = " ".join(str(p.get("title") or "").split())
    if not pid or not title:
        return None
    v = res.get("variantRollupValues") or {}
    sale = _first(v.get("inventory(847, price)")) or _first(v.get("price"))
    regular = _first(v.get("inventory(847, originalPrice)")) or _first(v.get("originalPrice"))
    savings = round(regular - sale, 2) if sale and regular and regular > sale else None
    attrs = p.get("attributes") or {}
    stmt_parts = (attrs.get("promotional_statement") or {}).get("text") or []
    stmt = " ".join(_TAG_RX.sub(" ", " ".join(str(x) for x in stmt_parts)).split())
    paths = [c for c in (p.get("categories") or []) if isinstance(c, str)]
    path = max(paths, key=lambda c: c.count(">")) if paths else ""
    pills = (attrs.get("pills") or {}).get("text") or []
    return {
        "title": title,
        "url": canonical_url(pid),
        "product_id": pid,
        "section": path.split(">")[0].strip() if path else "",
        "item_number": "",
        "limit": None,
        "tag": ", ".join(str(t) for t in pills),
        "list_sale": sale,
        "list_savings": savings,
        "list_regular": regular,
        "category_path": path,
        "promo_short": ", ".join(str(x) for x in (v.get("inventory(847, attributes.promotion_short_text)") or [])),
        "promo_text": stmt,
        "promo_end": promo_end(stmt),
        "source": "search",
    }


def has_discount_hint(item) -> bool:
    """The listing itself shows a real markdown (regular > sale > 0). Items without one are not worth a
    product-page scrape for NEW rows. DOM cards in the 'Save $N'-only form have no prices, so they pass on
    list_savings instead."""
    sale, reg = item.get("list_sale"), item.get("list_regular")
    if sale and reg:
        return reg > sale
    return bool(item.get("list_savings"))


def discount_pct(price, regular) -> float:
    """Confirmed markdown as a percentage of the regular price (0.0 when unknown / not a markdown)."""
    if not price or not regular or regular <= price:
        return 0.0
    return round((regular - price) / regular * 100, 1)


def classify_item(item, kw_compiled, path_compiled):
    """
    Category for a savings item. Search items carry Costco's own category path, so that decides
    (business.savings.paths: regex fragments + '!' excludes, matched against e.g.
    'Appliances > Small Kitchen Appliances > Air Fryers'). DOM cards have no path, so they fall back to the
    title keywords. A search item whose path matches nothing is off-category — the title is NOT consulted,
    so a grocery 'coffee' item never lands in Small Appliances on its name alone.
    """
    if item.get("source") == "search":
        return classify_category(item.get("category_path"), path_compiled)
    return classify_category(item.get("title"), kw_compiled)


def merge_items(*sources) -> list:
    """Union of items by product id. When both sources list a product the search version wins (it has
    the category path, promo dates and store prices); items without an id are kept as-is."""
    by_pid, order, loose = {}, [], []
    for items in sources:
        for it in items or []:
            pid = it.get("product_id")
            if not pid:
                loose.append(it)
            elif pid not in by_pid:
                by_pid[pid] = it
                order.append(pid)
            elif it.get("source") == "search" and by_pid[pid].get("source") != "search":
                by_pid[pid] = it
    return [by_pid[p] for p in order] + loose


# ── "already judged" cache: don't burn the scrape budget on the same rejects every day ─────────

def load_seen(path) -> dict:
    import json
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def recently_rejected(seen, pid, today, cooldown_days) -> bool:
    from datetime import datetime
    rec = seen.get(str(pid))
    if not rec or not cooldown_days:
        return False
    try:
        return (today - datetime.strptime(str(rec.get("date")), "%Y-%m-%d")).days < cooldown_days
    except ValueError:
        return False


def save_seen(path, seen, rejected, today, keep_days=30) -> dict:
    """Record definitive rejects {pid: reason} as of `today`, prune old entries, persist. Never raises."""
    import json
    import os
    from datetime import timedelta
    stamp = today.strftime("%Y-%m-%d")
    for pid, reason in rejected.items():
        seen[str(pid)] = {"date": stamp, "reason": reason}
    cutoff = (today - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if str(v.get("date", "")) >= cutoff}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(seen, f, indent=1)
    except OSError:
        pass
    return seen


# ── browser side ─────────────────────────────────────────────────────────────

def _offset_of(request):
    import json
    try:
        return int(json.loads(request.post_data or "{}").get("offset", 0))
    except (ValueError, TypeError):
        return None


def scrape_search_listing(page, url=SEARCH_URL, max_pages=70, refresh=None):
    """
    Crawl the 'OFF' search listing page by page (`&currentPage=N`), capturing each page's search-API
    response. Bounded by `max_pages` and by totalSize. `refresh(page)` runs every 20 pages (Costco's bot
    detection tolerates ~14 product pages; the session is refreshed on the scraper's usual cadence). Two
    consecutive failed pages end the crawl — partial results are still returned.
    Returns (items, meta): meta = {total, pages, failed, stopped}. Never raises.
    """
    import random
    items, meta = [], {"total": None, "pages": 0, "failed": 0, "stopped": ""}
    total_pages, n, consecutive_fail = max_pages, 1, 0
    while n <= min(total_pages, max_pages):
        expected = (n - 1) * SEARCH_PAGE_SIZE
        page_url = url if n == 1 else f"{url}&currentPage={n}"

        def is_this_page(r, expected=expected):
            return (SEARCH_API in r.url and r.request.method == "POST"
                    and _offset_of(r.request) == expected)
        try:
            if n > 1 and refresh and (n - 1) % 20 == 0:
                refresh(page)
            with page.expect_response(is_this_page, timeout=30000) as info:
                resp = page.goto(page_url, timeout=45000, wait_until="domcontentloaded",
                                 referer="https://www.costco.com/")
            if resp is not None and resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            sr = info.value.json().get("searchResult") or {}
            results = sr.get("results") or []
            if not results and n == 1:
                raise RuntimeError("no results on page 1")
            if meta["total"] is None:
                meta["total"] = int(sr.get("totalSize") or 0)
                total_pages = -(-meta["total"] // SEARCH_PAGE_SIZE) if meta["total"] else max_pages
            items.extend(it for it in (parse_search_result(r) for r in results) if it)
            meta["pages"] += 1
            consecutive_fail = 0
            if not results:
                meta["stopped"] = "empty page"
                break
        except Exception as e:
            meta["failed"] += 1
            consecutive_fail += 1
            logger.warning(f"  search page {n} failed: {e}")
            if consecutive_fail >= 2:
                meta["stopped"] = f"2 consecutive failures at page {n}"
                break
        n += 1
        page.wait_for_timeout(int(random.uniform(1200, 2600)))
    if not meta["stopped"] and total_pages > max_pages:
        meta["stopped"] = f"page budget {max_pages} of {total_pages}"
    logger.info(f"  search listing: {len(items)} items from {meta['pages']} pages "
                f"(total {meta['total']}, failed {meta['failed']}, {meta['stopped'] or 'complete'})")
    return items, meta
