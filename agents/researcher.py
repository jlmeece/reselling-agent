"""
Costco → eBay Research Agent — Autonomous Loop
================================================
WAT Framework: Agent layer for the research workflow.
Reads workflows/research.md for the SOP.

Full autonomous cycle:
  1. Discover new products by browsing Costco category pages
  2. Add newly found products to the sheet as PENDING (skip duplicates)
  3. For each PENDING product + any Tier 2 rechecks:
       a. Scrape Costco for current price and stock status
       b. Scrape eBay sold listings for real comp data
       c. Search Reddit communities for demand signals (freshness-checked)
       d. Ask Claude to score dimensions and generate 4-lens reasoning
       e. Run deterministic Pass 3 scoring math
       f. Write Tier score and summary to sheet
  4. Send Tier 1 digest email if any Tier 1s found

Jordan's approval workflow:
  PENDING  → newly discovered, waiting for Jordan to review
  APPROVED → Jordan approved, monitor cycle tracks it, copy generates
  WATCH    → Jordan wants to keep an eye on it, re-researched weekly
  ACTIVE   → listed and monitored
  PAUSED   → margin issue, hold
  URGENT   → needs immediate attention

Run: python agents/researcher.py
"""

import os
import sys
import json
import re
import yaml
import time
import random
from datetime import datetime, date
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)

import anthropic

from tools.sheet_writer import get_sheets_service, read_sheet, write_row_partial, append_row, append_rows_batch
from tools.costco_scraper import scrape_costco, get_cart_estimate, make_browser, refresh_session
from tools.costco_discovery import discover_all
from tools.ebay_research import get_ebay_comps
from tools.community_signals import get_community_signals
from tools.listing_copy import generate_listing_copy
from tools.alert_sender import send_alert
from tools.tier_scorer import score_product
from skills.research_gold import run_pass3 as gold_pass3
from skills.research_outdoor import run_pass3 as outdoor_pass3
from skills.research_watches import run_pass3 as watches_pass3
from skills.research_appliances import run_pass3 as appliances_pass3
from skills.research_pharmacy import run_pass3 as pharmacy_pass3
from skills.scoring import score_dimension
from skills.base_scoring import score_sell_through


def col_to_idx(col_letter: str) -> int:
    """Convert column letter to 0-based index: 'A'→0, 'B'→1, ..., 'Z'→25, 'AA'→26."""
    col_letter = col_letter.upper()
    result = 0
    for ch in col_letter:
        result = result * 26 + (ord(ch) - ord('A') + 1)
    return result - 1


# ── Config ────────────────────────────────────────────────────────

def _load_config():
    p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "categories.yaml")
    with open(p) as f:
        return yaml.safe_load(f)


def _load_col_map():
    p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "col_map.yaml")
    with open(p) as f:
        return yaml.safe_load(f)["columns"]


def _load_tier2_watchlist():
    p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tier2_watchlist.json")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return json.load(f)


def _save_tier2_watchlist(items):
    p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tier2_watchlist.json")
    with open(p, "w") as f:
        json.dump(items, f, indent=2)


def _append_tier3(entry):
    p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tier3_skipped.json")
    items = json.load(open(p)) if os.path.exists(p) else []
    if not isinstance(items, list):
        items = []
    items.append(entry)
    with open(p, "w") as f:
        json.dump(items, f, indent=2)


def _update_category_performance(category, tier):
    """Record a tier result for a category and recalculate its performance score."""
    p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "category_performance.json")
    data = json.load(open(p)) if os.path.exists(p) else {}

    cat = data.setdefault(category, {
        "tier1_count": 0, "tier2_count": 0, "tier3_count": 0,
        "total_researched": 0, "last_updated": "", "performance_score": 0.5,
    })

    cat[f"tier{tier}_count"] = cat.get(f"tier{tier}_count", 0) + 1
    cat["total_researched"]  = cat.get("total_researched", 0) + 1
    cat["last_updated"]      = date.today().isoformat()

    # Score: Tier 1 hits weighted 1.0, Tier 2 weighted 0.3, Tier 3 weighted 0
    total = cat["total_researched"]
    cat["performance_score"] = round(
        (cat["tier1_count"] * 1.0 + cat["tier2_count"] * 0.3) / total, 3
    )

    with open(p, "w") as f:
        json.dump(data, f, indent=2)

    logger.debug(f"  Performance updated — {category}: score={cat['performance_score']}")


# ── Sheet helpers ─────────────────────────────────────────────────

def _get_existing_urls(all_data):
    """Return set of Costco URLs already in the sheet (col R = index 17)."""
    urls = set()
    for row in all_data:
        if len(row) > 17 and row[17] and row[17].startswith("http"):
            urls.add(row[17].split("?")[0])
    return urls


def _add_new_products_batch(service, sheet_name, products, COL):
    """
    Appends all newly discovered products as PENDING rows in a single API call.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    col_value_dicts = []
    for product in products:
        col_value_dicts.append({
            COL["title"]:        product["title"],
            COL["category"]:     product["category"],
            COL["costco_url"]:   product["url"],
            COL["costco_cost"]:  product.get("price") or "",
            COL["status"]:       "PENDING",
            COL["last_checked"]: now,
            COL["tier_summary"]: "Discovered — awaiting research",
            COL["comp_saturation"]: "=IFERROR(M{ROW}/MAX(K{ROW},1),\"\")",
        })
    append_rows_batch(service, sheet_name, col_value_dicts)
    for product in products:
        logger.info(f"  Added PENDING: {product['title'][:50]}")
    logger.info(f"  {len(products)} products written in single batch call.")


# ── Claude research (Pass 1+2) ────────────────────────────────────

def _format_source_breakdown(bd: dict) -> str:
    """Compact one-line summary: 'reddit:r/Gold m=11/fresh.10/intent.18 ...'"""
    if not bd:
        return "no sources reported"
    parts = []
    # Sort by mentions desc, drop fully-quiet sources from the prompt
    for sid, info in sorted(bd.items(), key=lambda kv: -kv[1].get("mentions", 0))[:8]:
        if info.get("mentions", 0) == 0 and info.get("freshness", 0) == 0:
            continue
        parts.append(
            f"{sid} m={info.get('mentions',0)}/f={info.get('freshness',0):.2f}"
            f"/i={info.get('intent_rate',0):.2f}"
        )
    return ", ".join(parts) if parts else "all sources quiet"


RESEARCH_SYSTEM_PROMPT = """You are a sharp eBay reseller analyst specializing in Costco products.
Score this product across five dimensions (0-10 each) using the real market data provided.
Be critical — most products don't deserve top scores. A score of 7+ means "list immediately."

Dimensions:
- margin_potential: based on actual Costco cost vs eBay avg sold price provided
- demand_signals: use the eBay sold count and community signal strength provided
- competition_density: use the active eBay listing count provided (fewer = higher score)
- costco_availability: DO NOT score this — it will be overridden with live data
- fulfillment_risk: size/weight/fragility of this specific product type

Four lenses (1-2 sentences each, use the real data in your reasoning):
- conservative_analyst: downside, exit risk, margin safety
- growth_operator: upside if trend holds, demand momentum
- brand_builder: category authority, repeat buyer potential
- volume_flipper: speed, ease of listing, turnover rate

Return ONLY raw JSON starting with { and ending with }. No markdown."""


def _run_claude_research(title, category, costco_cost, ebay_price,
                          stock_status, fee_rate, category_notes,
                          ebay_data, community_data, spot_data=None):
    """Pass 1+2: Claude scores the product using real eBay + community data."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Calculate actual margin
    margin_text = "unknown"
    try:
        cost  = float(str(costco_cost).replace("$", "").replace(",", ""))
        price = float(str(ebay_price or ebay_data.get("avg_sold_price") or 0)
                      .replace("$", "").replace(",", ""))
        if cost and price:
            net = price - cost - price * fee_rate
            margin_text = f"{net / price * 100:.1f}% (${net:.0f} per unit)"
    except (ValueError, TypeError):
        pass

    user_content = (
        f"Research this Costco product:\n\n"
        f"TITLE: {title}\n"
        f"CATEGORY: {category}\n"
        f"COSTCO COST: ${costco_cost}\n"
        f"EBAY TARGET PRICE: ${ebay_price or 'not set'}\n"
        f"STOCK STATUS: {stock_status}\n"
        f"CALCULATED MARGIN: {margin_text}\n"
        f"EBAY FEE RATE: {fee_rate * 100:.1f}%\n"
        f"CATEGORY NOTES: {category_notes}\n\n"
        f"REAL EBAY DATA:\n"
        f"  Sold last 90 days: {ebay_data.get('sold_90d', 'unknown')}\n"
        f"  Median sold price: ${ebay_data.get('median_sold', 'unknown')}\n"
        f"  Avg sold price: ${ebay_data.get('avg_sold_price', 'unknown')}\n"
        f"  Active competing listings: {ebay_data.get('active_count', 'unknown')}\n"
        f"  Median active listing price: ${ebay_data.get('median_active', 'unknown')}\n"
        f"  Sold price range: {ebay_data.get('sold_range', 'unknown')}\n"
        f"  Search query used: \"{ebay_data.get('query_used', '')}\"\n\n"
        + (
            f"PRECIOUS METALS DATA:\n"
            f"  Metal: {spot_data.get('metal', 'gold').title()}\n"
            f"  Spot price: ${spot_data['spot_price']:.2f}/oz\n"
            f"  Product weight: {spot_data['weight_oz']:.4f} troy oz\n"
            f"  Karat/purity: {spot_data['karat']}kt\n"
            f"  Melt value: ${spot_data['melt_value']:.2f}\n"
            f"  eBay premium above melt: {spot_data['premium_pct']:+.1f}%\n"
            f"  Note: buyers pay premium for Costco trust, mint brand, and convenience — not just melt value.\n"
            f"  IMPORTANT: Sold 90-day median may be stale. Use median active listing price as the primary eBay price signal.\n\n"
            if spot_data else ""
        )
        + f"COMMUNITY SIGNAL: {community_data.get('signal_strength', 0):.1f}/10\n"
        f"  {community_data.get('summary', '')}\n"
        f"  Recent posts: {len(community_data.get('recent_posts', []))}\n"
        f"  Source breakdown: {_format_source_breakdown(community_data.get('source_breakdown', {}))}\n"
        f"  Purchase-intent phrases: {('; '.join(community_data.get('intent_phrases', [])[:5])) or 'none'}\n\n"
        "Return JSON:\n"
        "{\n"
        "  \"dimension_scores\": {\n"
        "    \"margin_potential\": <0-10>,\n"
        "    \"demand_signals\": <0-10>,\n"
        "    \"competition_density\": <0-10>,\n"
        "    \"costco_availability\": 5,\n"
        "    \"fulfillment_risk\": <0-10>\n"
        "  },\n"
        "  \"reasoning\": {\n"
        "    \"conservative_analyst\": \"<uses real margin/sold data>\",\n"
        "    \"growth_operator\": \"<uses real sold count/trend>\",\n"
        "    \"brand_builder\": \"<uses real community signal>\",\n"
        "    \"volume_flipper\": \"<uses real active count>\"\n"
        "  },\n"
        "  \"search_terms\": [\"<5 actual eBay search terms>\"],\n"
        "  \"demand_note\": \"<one sentence risk or opportunity worth watching>\"\n"
        "}"
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=900,
        system=[{
            "type": "text",
            "text": RESEARCH_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    # Find the first {...} block in case Claude adds prose before/after
    brace_start = raw.find("{")
    brace_end   = raw.rfind("}")
    if brace_start != -1 and brace_end != -1:
        raw = raw[brace_start:brace_end + 1]

    parsed = json.loads(raw.strip())

    # Normalize: Claude sometimes returns dimension scores flat at the top level
    # instead of nested under "dimension_scores". Reconstruct the wrapper if needed.
    _DIM_KEYS = {"margin_potential", "demand_signals", "competition_density",
                 "costco_availability", "fulfillment_risk"}
    if "dimension_scores" not in parsed and _DIM_KEYS & parsed.keys():
        parsed["dimension_scores"] = {k: parsed.pop(k) for k in _DIM_KEYS if k in parsed}

    # Validate required keys so callers get a clear error, not a confusing KeyError
    required = {"dimension_scores", "reasoning"}
    missing  = required - parsed.keys()
    if missing:
        raise ValueError(
            f"Claude response missing required keys {missing}. "
            f"Got keys: {list(parsed.keys())}. Raw: {raw[:300]}"
        )
    return parsed


def _ship_badge(free_shipping: bool, cart_est: dict) -> str:
    if free_shipping:
        return "✓ FREE"
    ship = cart_est.get("shipping")
    if ship is not None:
        if ship <= 0:
            return "✓ FREE"
        return f"${ship:.2f} ship"
    return ""


def _suggest_ebay_price(costco_cost, ebay_data: dict, fee_rate: float) -> float | None:
    """
    Data-backed eBay listing price suggestion.
    Anchor priority: median sold → median active → avg sold → cost-based fallback.

    Always returns a price when cost is known — never leaves Col H blank.
    Fallback when eBay data is sparse: cost × 1.30 (30% markup floor).
    Jordan cannot review products without a price in Col H.
    """
    try:
        cost = float(str(costco_cost).replace("$", "").replace(",", ""))
    except (ValueError, TypeError):
        cost = 0

    anchor = (
        ebay_data.get("median_sold")
        or ebay_data.get("median_active")
        or ebay_data.get("avg_sold_price")
    )

    if anchor:
        price = float(anchor)

        # Sanity check: if eBay market is far below Costco cost, flag for manual review.
        # We still use the market price — Jordan decides whether to buy, not a margin floor.
        if cost > 0 and price < cost * 0.80:
            logger.warning(
                f"  _suggest_ebay_price: eBay anchor ${price:.2f} is < 80% of cost ${cost:.2f}. "
                f"Possible wrong-product match or negative margin — flagging."
            )
            ebay_data["wrong_product_flag"] = True
            ebay_data["note"] = (
                (ebay_data.get("note") or "")
                + f"⚠️ VERIFY: eBay avg ${price:.0f} vs cost ${cost:.0f} — "
                "negative margin or possible wrong product match. Check manually. "
            )

        sold   = ebay_data.get("sold_90d") or 0
        active = ebay_data.get("active_count") or 0
        if sold > 0 and active > sold * 2:
            price *= 0.97  # crowded market — price 3% below median

        if cost > 0:
            # No margin floor — market sets the price. Col I (net_profit) shows the reality.
            # Only cap at 3× cost (clearly wrong data if above that).
            max_price = cost * 3.0
            if price > max_price:
                logger.warning(
                    f"  _suggest_ebay_price: ${price:.2f} exceeds 3× cost cap (${max_price:.2f}). Capping."
                )
                price = max_price
    elif cost > 0:
        # No eBay data at all — use cost × 1.30 as a starting point.
        # This gives Jordan something to work with. Mark it as an estimate.
        price = cost * 1.30
        ebay_data["price_basis"]    = "cost×1.30-estimate"
        ebay_data["query_strategy"] = "no-ebay-data"
        logger.info(f"  _suggest_ebay_price: no eBay data — using cost×1.30 fallback (${price:.2f})")
    else:
        return None  # no cost, no eBay data — genuinely can't price

    price = round(price) - 0.01
    return price if price >= 1 else None


def _pass3_for_category(category):
    if category.lower() in ("jewelry", "gold"):
        return gold_pass3
    if category.lower() == "watches":
        return watches_pass3
    if category.lower() == "small appliances":
        return appliances_pass3
    if category.lower() == "pharmacy":
        return pharmacy_pass3
    return outdoor_pass3


def _build_niche_key(title):
    """Reduce a product title to its core niche identifier for duplicate detection."""
    GENERIC = {"kirkland", "signature", "costco", "set", "pack", "count", "piece",
               "bottles", "oz", "fl", "mg", "ct"}
    words = re.sub(r'[^\w\s]', ' ', title.lower()).split()
    key_words = [w for w in words if w not in GENERIC and not w.isdigit()][:3]
    return "-".join(key_words)


# ── Main research loop ────────────────────────────────────────────

def run_researcher(limit=None, add_limit=None, category_filter=None, discover_only=False, skip_discovery=False):
    """
    limit:            max products to score this run (None = all PENDING)
    add_limit:        max new products to add to sheet during discovery (None = all)
    category_filter:  only research this category (e.g. 'Jewelry')
    discover_only:    add new products to sheet but skip scoring
    skip_discovery:   skip Costco scraping, go straight to researching PENDING rows
    """
    config   = _load_config()
    COL      = _load_col_map()
    business = config["business"]
    categories = config["categories"]

    logger.info(f"Researcher started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    _run_start = time.time()

    service    = get_sheets_service()
    sheet_name = business["sheet_name"]
    start_row  = business["data_start_row"]
    end_row    = business["data_end_row"]

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")

    # ── Step 1: Discover new products ────────────────────────────
    if skip_discovery:
        logger.info("Step 1: Skipping discovery (--skip-discovery flag set).")
        new_products = []
    else:
        existing_urls = _get_existing_urls(all_data)
        logger.info("Step 1: Discovering products on Costco...")
        with make_browser() as costco_page:
            discovered = discover_all(costco_page, categories, category_filter=category_filter)

        new_products = [p for p in discovered if p["url"] not in existing_urls]
        logger.info(f"  {len(discovered)} found, {len(new_products)} are new.")

        if add_limit is not None and len(new_products) > add_limit:
            logger.info(f"  Capping at {add_limit} new products (found {len(new_products)}).")
            new_products = new_products[:add_limit]

        # Add new products to sheet as PENDING (single batch API call)
        if new_products:
            _add_new_products_batch(service, sheet_name, new_products, COL)

        if discover_only:
            logger.info(f"Discover-only mode — {len(new_products)} products added. Skipping research.")
            return

        # Reload sheet data now that new rows are added
        all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")

    # ── Step 2: Identify rows needing research ────────────────────
    tier2_watchlist = _load_tier2_watchlist()
    tier2_recheck_urls = {
        item["costco_url"]
        for item in tier2_watchlist
        if isinstance(item, dict) and item.get("recheck_date", "") <= date.today().isoformat()
    }
    today_str = date.today().isoformat()

    def safe_get(lst, i, default=""):
        return lst[i] if i < len(lst) else default

    to_research = []
    for idx, row in enumerate(all_data):
        if not row or not row[0]:
            continue
        # New column layout: A=status(0), B=tier(1), C=title(2), D=category(3),
        #   R=costco_url(17), S=re_eval_date(18)
        status       = safe_get(row, 0)   # col A
        demand_score = safe_get(row, 1)   # col B
        category     = safe_get(row, 3)   # col D
        costco_url   = safe_get(row, 17)  # col R
        re_eval_date = safe_get(row, 18)  # col S

        if not costco_url or not costco_url.startswith("http"):
            continue
        if category_filter and category != category_filter:
            continue

        paused_due = (
            status == "PAUSED" and
            re_eval_date and
            re_eval_date <= today_str
        )
        needs_research = (
            status == "PENDING" or
            (status == "WATCH" and costco_url in tier2_recheck_urls) or
            paused_due or
            (not demand_score and status not in ("APPROVED", "ACTIVE", "URGENT", "PAUSED"))
        )
        if needs_research:
            to_research.append((idx + start_row, row))

    # Apply run limit — prioritize gold bars and precious metals
    if limit and len(to_research) > limit:
        logger.info(f"  Limiting to {limit} products (queue has {len(to_research)}).")
        to_research = to_research[:limit]

    logger.info(f"Step 2: {len(to_research)} product(s) queued for research.")

    # ── Fetch spot prices once per run (Precious Metals + Jewelry) ──
    from tools.spot_price import get_spot_price, melt_value, spot_premium_pct, parse_gold_weight
    _spot_prices = {}
    if any(safe_get(r, 3) in ("Jewelry", "Precious Metals") for _, r in to_research):
        for metal in ("gold", "silver", "platinum"):
            _spot_prices[metal] = get_spot_price(metal)
        logger.info(f"  Spot prices: gold=${_spot_prices.get('gold')}/oz  silver=${_spot_prices.get('silver')}/oz")

    # ── Step 3: Research each product ────────────────────────────
    tier1_results = []
    new_tier2 = []

    # Costco session guard: halt after this many consecutive CHECK FAILED results.
    # Costco rate-limits the scraper after ~14 product page hits in one session.
    # At 2 consecutive fails we attempt a session refresh; at 3 we stop entirely
    # rather than scoring 40+ products with missing data.
    MAX_CONSECUTIVE_CHECK_FAILS = 3
    consecutive_check_fails = 0
    consecutive_ebay_fails  = 0
    _ebay_fail_log          = []   # titles of products whose eBay fetch failed
    researched_count = 0

    _seen_niches: set = set()
    with make_browser() as costco_page:
        for _product_idx, (sheet_row, row) in enumerate(to_research):
            # Refresh Costco session every 5 products — empirically the session
            # dies around product 5-7 if left alone (rate-limit / cookie expiry).
            if _product_idx > 0 and _product_idx % 5 == 0:
                refresh_session(costco_page)
                consecutive_check_fails = 0  # reset after intentional refresh

            title        = safe_get(row, 2)   # col C
            category     = safe_get(row, 3)   # col D
            costco_url   = safe_get(row, 17)  # col R
            costco_cost  = safe_get(row, 6)   # col G
            ebay_price   = safe_get(row, 7)   # col H

            # Near-duplicate niche detection
            niche_key = f"{category}:{_build_niche_key(title)}"
            if niche_key in _seen_niches:
                logger.warning(f"  ⚠️ Near-duplicate niche already researched this run: {niche_key} — consider consolidating listings")
            _seen_niches.add(niche_key)

            if not costco_url:
                continue

            cat_config = categories.get(category, {})
            fee_rate   = cat_config.get("fee_rate", 0.1325)

            logger.info(f"Researching row {sheet_row}: {title[:50]}...")

            # 3a. Scrape Costco for current stock + price + brand/model
            costco_data    = scrape_costco(costco_url, page=costco_page)
            stock_status   = costco_data["stock_status"]
            live_price     = costco_data["price"]
            brand          = costco_data.get("brand")
            model          = costco_data.get("model")
            in_stock       = costco_data.get("in_stock", False)
            purchase_limit = costco_data.get("purchase_limit")
            on_sale        = costco_data.get("on_sale", False)
            sale_savings   = costco_data.get("sale_savings")
            sale_expires   = costco_data.get("sale_expires")
            free_shipping  = costco_data.get("free_shipping", False)
            if live_price and not costco_cost:
                costco_cost = live_price

            # Consecutive CHECK FAILED guard — try session refresh, but never halt the queue.
            # Aborting here would leave 30+ products with zero eBay comps and blank prices.
            # Instead: refresh on 2 consecutive fails, log on 3+, and always continue.
            if stock_status == "CHECK FAILED":
                consecutive_check_fails += 1
                if consecutive_check_fails == 2:
                    logger.warning("  2 consecutive CHECK FAILED — attempting session refresh...")
                    refresh_session(costco_page)
                elif consecutive_check_fails >= MAX_CONSECUTIVE_CHECK_FAILS:
                    logger.warning(
                        f"  {consecutive_check_fails} consecutive CHECK FAILED — "
                        f"Costco session may be dead but continuing queue to fill eBay data. "
                        f"Run python tools/setup_costco_session.py to restore Costco scraping."
                    )
                    # Use existing cost from sheet if we have it — eBay comps can still run
                    if not costco_cost:
                        write_row_partial(service, sheet_name, sheet_row, [
                            (COL["stock_status"],  "CHECK FAILED"),
                            (COL["last_checked"],  datetime.now().strftime("%Y-%m-%d %H:%M")),
                        ])
                        # No cost at all — can't price. Skip this product but keep going.
                        continue
            else:
                consecutive_check_fails = 0

            # Enrich stock label for Precious Metals using config purchase limits.
            # Costco shows "Limited Quantity" for all precious metals (policy label, not low stock).
            # We translate "Available (limited)" → "Available (N/day limit)" using the config.
            if category == "Precious Metals" and stock_status == "Available (limited)":
                pm_limits = cat_config.get("purchase_limits", {})
                t = title.lower()
                if "silver" in t:
                    daily = pm_limits.get("silver_bar_10oz")
                elif "coin" in t:
                    daily = pm_limits.get("gold_coin_1oz")
                elif "100g" in t or "100 g" in t:
                    daily = pm_limits.get("gold_bar_100g")
                else:
                    daily = pm_limits.get("gold_bar_1oz")  # 1oz bar default
                if daily:
                    stock_status = f"Available ({daily}/day limit)"
                    if not purchase_limit:
                        purchase_limit = daily

            # Spot price context for precious metals
            spot_data = None
            if category in ("Precious Metals", "Jewelry") and _spot_prices.get("gold"):
                w_oz, karat = costco_data.get("weight_oz"), costco_data.get("karat")
                if w_oz is None:
                    w_oz, karat = parse_gold_weight(title)
                if w_oz:
                    # Detect metal from title — default gold, override for silver/platinum
                    title_lower = title.lower()
                    if "silver" in title_lower:
                        metal = "silver"
                    elif "platinum" in title_lower:
                        metal = "platinum"
                    else:
                        metal = "gold"
                    k = karat or 24
                    spot_for_metal = _spot_prices.get(metal) or _spot_prices.get("gold")
                    mv = melt_value(w_oz, metal, k)
                    ebay_median = None  # filled after eBay comps
                    spot_data = {
                        "spot_price": spot_for_metal,
                        "metal":      metal,
                        "weight_oz":  w_oz,
                        "karat":      k,
                        "melt_value": mv or 0,
                        "premium_pct": None,  # computed after eBay comps
                    }

            # WAREHOUSE ONLY — skip research entirely, not fulfillable by dropship
            if stock_status == "WAREHOUSE ONLY":
                logger.info(f"  Skipping {title[:40]!r} — warehouse only, can't dropship")
                write_row_partial(service, sheet_name, sheet_row, [
                    (COL["stock_status"],  "WAREHOUSE ONLY"),
                    (COL["last_checked"],  datetime.now().strftime("%Y-%m-%d %H:%M")),
                    (COL["tier_summary"],  "Warehouse-only — not available for online dropship"),
                ])
                continue

            # 3a-ii. Cart estimate (tax + Costco shipping) — only if in stock
            cart_est: dict = {}
            if in_stock:
                try:
                    cart_est = get_cart_estimate(costco_url, page=costco_page)
                except Exception as e:
                    logger.warning(f"  Cart estimate failed: {e}")

            # Log cart estimate failure reason for debugging
            if not cart_est or (not cart_est.get("shipping") and not cart_est.get("free_shipping")):
                reason = cart_est.get("reason", "unknown") if cart_est else "exception"
                logger.debug(f"  Cart estimate unavailable ({reason}) — ship cost will be empty in sheet")

            time.sleep(1)

            # 3b. eBay comp research — with one retry and a consecutive-failure halt.
            # If eBay times out or returns garbage 3 times in a row, stop rather than
            # scoring products with no market data (produces unreliable prices/scores).
            _ebay_empty = {"sold_90d": None, "avg_sold_price": None,
                           "active_count": None, "fee_rate": fee_rate,
                           "query_used": "", "query_strategy": "error"}
            ebay_fetch_ok = False
            for _attempt in range(2):   # try once, retry once on failure
                try:
                    ebay_data = get_ebay_comps(
                        title, category, page=costco_page,
                        brand=brand, model=model,
                        ebay_category_id=cat_config.get("ebay_category_id"),
                    )
                    ebay_fetch_ok = True
                    consecutive_ebay_fails = 0
                    break
                except Exception as e:
                    logger.warning(f"  eBay research failed (attempt {_attempt + 1}/2): {e}")
                    if _attempt == 0:
                        time.sleep(8)   # brief pause before retry
            if not ebay_fetch_ok:
                ebay_data = dict(_ebay_empty, note="eBay fetch failed after retry")
                consecutive_ebay_fails += 1
                _ebay_fail_log.append(title[:60])
                if consecutive_ebay_fails >= MAX_CONSECUTIVE_CHECK_FAILS:
                    logger.warning(
                        f"  {consecutive_ebay_fails} consecutive eBay failures (last: '{title[:40]}') — "
                        f"continuing with cost-based pricing. Will not halt queue."
                    )
                    # Do NOT break — the queue continues and cost×1.30 fallback fills Col H

            # Fill eBay premium into spot_data now that we have comps
            if spot_data and spot_data.get("melt_value"):
                ebay_ref = ebay_data.get("median_sold") or ebay_data.get("median_active")
                if ebay_ref:
                    spot_data["premium_pct"] = spot_premium_pct(
                        ebay_ref, spot_data["weight_oz"],
                        metal=spot_data.get("metal", "gold"),
                        karat=spot_data["karat"]
                    )

            # Suggested listing price — fill col H when not already set
            suggested_price = _suggest_ebay_price(costco_cost, ebay_data, fee_rate)

            # Jewelry-specific wrong_product_flag: eBay median far below Costco cost signals wrong comps
            if category and "jewelry" in category.lower() and costco_cost:
                try:
                    median_price = ebay_data.get("median_sold") or ebay_data.get("median_active") or 0
                    if median_price and median_price < float(str(costco_cost).replace("$","").replace(",","")) * 0.20:
                        ebay_data["wrong_product_flag"] = True
                        ebay_data["note"] = (
                            (ebay_data.get("note") or "")
                            + f"⚠️ eBay median (${median_price:.0f}) << Costco cost (${costco_cost}) — likely wrong comps, verify manually"
                        )
                except (TypeError, ValueError):
                    pass

            # Precious metals fallback: when eBay comps return no usable median
            # (common for $3k+ gold bars), estimate from melt value × target premium.
            # Costco gold bars typically sell 3–8% above spot on eBay; 5% is conservative.
            if not suggested_price and spot_data and spot_data.get("melt_value"):
                target_premium = 0.05
                fallback = spot_data["melt_value"] * (1 + target_premium)
                try:
                    cost_f = float(str(costco_cost).replace("$", "").replace(",", ""))
                    min_price = cost_f / (1 - fee_rate - 0.10)
                    if fallback >= min_price:
                        suggested_price = round(fallback) - 0.01
                        ebay_data["price_basis"]    = "melt×1.05"
                        ebay_data["query_strategy"] = "spot-fallback"
                except (ValueError, TypeError):
                    pass

            # Sanity check: don't suggest a price more than 10% above actual eBay avg.
            # When eBay comps matched the wrong product class (e.g. silver instead of gold),
            # avg_sold_price exposes the mismatch. Cap at avg × 1.05 and flag the discrepancy.
            avg_sold = ebay_data.get("avg_sold_price")
            if suggested_price and avg_sold:
                try:
                    avg_f = float(str(avg_sold).replace("$", "").replace(",", ""))
                    if avg_f > 0 and suggested_price > avg_f * 1.10:
                        logger.warning(
                            f"  Price sanity: suggested ${suggested_price:.2f} is "
                            f"{(suggested_price/avg_f - 1)*100:.0f}% above eBay avg ${avg_f:.2f}. "
                            f"Capping at avg × 1.05."
                        )
                        suggested_price = round(avg_f * 1.05) - 0.01
                        ebay_data["price_basis"] = "ebay-avg×1.05 (capped)"
                except (ValueError, TypeError):
                    pass

            if suggested_price and not ebay_price:
                ebay_price = suggested_price

            time.sleep(random.uniform(1, 2))

            # 3c. Community signals (multi-source, brand/model-aware)
            try:
                community_data = get_community_signals(
                    title, category, cat_config,
                    brand=brand, model=model, costco_url=costco_url,
                )
            except Exception as e:
                logger.warning(f"  Community signals failed: {e}")
                community_data = {"signal_strength": 1, "recent_posts": [],
                                   "stale_sources": [], "active_sources": [],
                                   "source_breakdown": {}, "intent_phrases": [],
                                   "summary": f"Research failed: {e}"}

            # 3d. Claude scoring
            try:
                claude_result = _run_claude_research(
                    title, category, costco_cost, ebay_price,
                    stock_status, fee_rate,
                    cat_config.get("notes", ""),
                    ebay_data, community_data,
                    spot_data=spot_data,
                )
            except Exception as e:
                logger.error(f"  Claude research failed: {e}")
                write_row_partial(service, sheet_name, sheet_row, [
                    (COL["tier_summary"], f"Research failed: {e}"),
                ])
                continue

            dimension_scores = claude_result["dimension_scores"]
            reasoning        = claude_result["reasoning"]

            # Override availability with live scraped status
            # "Available (N/day limit)" strings use "Available" prefix — map those to Limited (6)
            avail_map = {"In Stock": 10, "Limited": 6, "OUT OF STOCK": 0,
                         "Unknown": 3, "CHECK FAILED": 1}
            if stock_status and stock_status.startswith("Available"):
                avail_score = 6  # in-stock with purchase limit ≈ Limited
            else:
                avail_score = avail_map.get(stock_status, 3)
            dimension_scores["costco_availability"] = avail_score

            # Incorporate community signal into demand score (weighted blend)
            if community_data["signal_strength"] > 1:
                claude_demand = dimension_scores.get("demand_signals", 5)
                community_score = community_data["signal_strength"]
                # 70% eBay data, 30% community signal
                dimension_scores["demand_signals"] = round(
                    claude_demand * 0.70 + community_score * 0.30, 1
                )

            # Blend sell-through rate into demand score
            _st_sold = ebay_data.get("sold_90d") or 0
            active_count_val = ebay_data.get("active_count") or 0
            st_score = score_sell_through(_st_sold, active_count_val)
            if st_score > 5:
                # Only boost if sell-through is above neutral — don't penalize low-signal products
                current_demand = dimension_scores.get("demand_signals", 5)
                dimension_scores["demand_signals"] = round(
                    current_demand * 0.70 + st_score * 0.30, 1
                )

            # Precious Metals: override two dimensions with spot-price-aware logic.
            # Generic scoring breaks for gold bars because:
            #   - margin_potential uses stale eBay sold comps (90d old = gold was cheaper)
            #   - competition_density penalizes 47-462 active listings as "crowded"
            #     but for a liquid commodity, that's normal market depth, not competition
            if category == "Precious Metals" and spot_data and spot_data.get("melt_value"):
                try:
                    raw_cost = float(str(live_price or costco_cost).replace("$", "").replace(",", ""))
                    spot_premium_pct_val = (raw_cost - spot_data["melt_value"]) / spot_data["melt_value"]
                    if spot_premium_pct_val < 0.02:
                        dimension_scores["margin_potential"] = 8   # within 2% of spot — great entry
                    elif spot_premium_pct_val < 0.05:
                        dimension_scores["margin_potential"] = 6
                    elif spot_premium_pct_val < 0.10:
                        dimension_scores["margin_potential"] = 4
                    else:
                        dimension_scores["margin_potential"] = 2   # >10% over spot — bad entry
                except (ValueError, TypeError, ZeroDivisionError):
                    pass
                # Active listing count = market liquidity for a commodity, not crowding
                dimension_scores["competition_density"] = 5  # neutral

            # 3e. Pass 3: deterministic scoring
            pass3_fn = _pass3_for_category(category)
            result   = pass3_fn(dimension_scores, cat_config, reasoning=reasoning)

            tier           = result["tier"]
            weighted_score = result["weighted_score"]
            logger.info(f"  → Tier {tier} | Score {weighted_score} | {stock_status}")

            # Build sheet notes summary — summary header + Tier line + community breakdown
            top_lens = max(result["lens_scores"], key=result["lens_scores"].get)
            demand_note = claude_result.get("demand_note", "")
            comm_summary = community_data.get("summary", "")
            comm_breakdown = _format_source_breakdown(
                community_data.get("source_breakdown", {})
            )
            intent_seen = community_data.get("intent_phrases", []) or []
            price_note = ""
            if suggested_price:
                basis     = ebay_data.get("price_basis", "?")
                strategy  = ebay_data.get("query_strategy", "title")
                price_note = f" | Suggested eBay: ${suggested_price:.2f} (basis: {basis}, search: {strategy})"

            # ── Col T: short one-liner (always first thing Jordan sees) ──────────────
            margin_str = ""
            if suggested_price and costco_cost:
                try:
                    cost_f = float(str(costco_cost).replace("$", "").replace(",", ""))
                    net_f  = suggested_price - cost_f - suggested_price * fee_rate
                    margin_str = f" | ~{net_f / suggested_price * 100:.1f}%"
                except (ValueError, TypeError):
                    pass
            price_summary = f"Sugg: ${suggested_price:,.2f}{margin_str} | " if suggested_price else ""

            # Sale / shipping flags — boost score for on-sale items
            if on_sale:
                weighted_score = round(min(10.0, weighted_score + 0.5), 2)

            verify_flag = " ⚠️VERIFY" if ebay_data.get("wrong_product_flag") else ""

            # Velocity + monthly profit estimate — the real opportunity signal
            sold_90d_val   = ebay_data.get("sold_90d") or 0
            monthly_units  = round(sold_90d_val / 3, 1)
            # Cap velocity by purchase limit — you can only buy what Costco allows
            if purchase_limit:
                try:
                    limit_per_day = int(str(purchase_limit).replace("/day", "").strip())
                    max_monthly   = limit_per_day * 30
                    monthly_units = min(monthly_units, max_monthly)
                except (ValueError, TypeError):
                    pass
            if suggested_price and costco_cost:
                try:
                    _sp = float(suggested_price)
                    _c  = float(str(costco_cost).replace("$", "").replace(",", ""))
                    _net = round(_sp * (1 - fee_rate) - _c, 2)
                    _monthly_profit = round(_net * monthly_units, 0)
                    velocity_str = f" | {monthly_units}/mo → ${_monthly_profit:,.0f}/mo"
                except Exception:
                    velocity_str = f" | {monthly_units}/mo"
            else:
                velocity_str = f" | {monthly_units}/mo" if monthly_units else ""

            tier_summary_line = (
                f"[T{tier} | Score {weighted_score} | {price_summary}Costco: {costco_url}"
                f"{velocity_str}{verify_flag}]"
            )

            # ── Col AV: full research narrative (hidden) ───────────────────────────
            full_notes = (
                f"Tier {tier} (score {weighted_score}) | {result['recommendation']} | "
                f"Strongest lens: {top_lens.replace('_', ' ').title()} | {demand_note}"
                f"{price_note}\n"
                f"Community: {comm_summary}\n"
                f"Sources: {comm_breakdown}"
            )
            if intent_seen:
                full_notes += "\nIntent phrases: " + " | ".join(intent_seen[:3])
            if spot_data and spot_data.get("melt_value"):
                prem = f"{spot_data['premium_pct']:+.1f}%" if spot_data.get("premium_pct") is not None else "unknown"
                full_notes += (
                    f"\n{spot_data.get('metal', 'gold').title()} spot: ${spot_data['spot_price']:.2f}/oz | "
                    f"Weight: {spot_data['weight_oz']:.4f} oz | "
                    f"Melt: ${spot_data['melt_value']:.2f} | "
                    f"eBay premium above melt: {prem}"
                )
            if on_sale and sale_savings:
                exp_str = f", expires {sale_expires}" if sale_expires else ""
                full_notes += f"\n🔥 ON SALE: ${sale_savings:.0f} off{exp_str} — time-limited flip opportunity (+0.5 score boost)"
            if free_shipping:
                full_notes += "\n📦 FREE SHIPPING from Costco — higher margin potential vs. paid-ship comps"
            if purchase_limit:
                full_notes += f"\nPurchase limit: {purchase_limit}/day — list max {purchase_limit} units on eBay"
            if cart_est:
                ship_str = f"${cart_est['shipping']:.2f}" if cart_est.get("shipping") is not None else "?"
                tax_str  = f"${cart_est['tax']:.2f}" if cart_est.get("tax") is not None else "?"
                full_notes += f"\nCostco cart est: ship={ship_str} tax={tax_str}"
                if cart_est.get("delivery_window"):
                    full_notes += f" | delivery: {cart_est['delivery_window']}"

            # ── Col X: sale badge; Col Y: free ship badge ──────────────────────────
            sale_info_val = ""
            if on_sale:
                sale_info_val = f"🔥 -${sale_savings:.0f}" if sale_savings else "🔥 SALE"
                if sale_expires:
                    sale_info_val += f" ends {sale_expires}"
            free_ship_val = _ship_badge(free_shipping, cart_est)

            # Skip if no eBay price could be determined
            if not ebay_price or float(str(ebay_price).replace("$", "").replace(",", "") or 0) <= 0:
                logger.warning(f"  Skipping {title[:50]} — no eBay price data, not writing to sheet.")
                continue

            # 3f. Write to sheet
            from datetime import timedelta
            recheck_date_str = (date.today() + timedelta(days=30)).isoformat()
            updates = [
                (COL["demand_score"],  weighted_score),
                (COL["stock_status"],  stock_status),       # col F — stock only, no badges
                (COL["last_checked"],  datetime.now().strftime("%Y-%m-%d %H:%M")),
                (COL["tier_summary"],  tier_summary_line),  # col T — short one-liner
                (COL["full_notes"],    full_notes),          # col AV — full narrative (hidden)
                (COL["sale_info"],     sale_info_val),       # col X — sale badge
                (COL["free_shipping"], free_ship_val),       # col Y — free ship badge
                (COL["fee_rate"],      fee_rate),            # col AA — needed for =H*AA (eBay fees formula)
            ]
            if ebay_data.get("sold_90d") is not None:
                updates.append((COL["sold_90d"],   ebay_data["sold_90d"]))
            if ebay_data.get("avg_sold_price") is not None:
                updates.append((COL["avg_price"],  ebay_data["avg_sold_price"]))
            if ebay_data.get("active_count") is not None:
                updates.append((COL["comp_count"], ebay_data["active_count"]))
            if live_price:
                updates.append((COL["costco_cost"], live_price))
            # Write suggested price to col H (once) and col V (permanent record, never overwrite)
            if suggested_price and not safe_get(row, 7):
                updates.append((COL["ebay_price"], suggested_price))
            if suggested_price and not safe_get(row, col_to_idx(COL["suggested_price"])):
                updates.append((COL["suggested_price"], suggested_price))
            if purchase_limit:
                updates.append((COL["purchase_limit"], f"{purchase_limit}/day"))
            # Update status so researched rows don't get re-queued every run
            # Tier 1 stays PENDING (Jordan email sent; Jordan changes to APPROVED manually)
            if tier == 2:
                updates.append((COL["status"], "WATCH"))
            elif tier == 3:
                updates.append((COL["status"], "PAUSED_DEMAND"))
                updates.append((COL["re_eval_date"], recheck_date_str))
            # Cart estimate — ship_cost → col AD; tax_est → col AF (formula column, overwrite OK here)
            if cart_est.get("shipping") is not None:
                updates.append((COL["ship_cost"], cart_est["shipping"]))
            if cart_est.get("tax") is not None:
                updates.append((COL["tax_est"], cart_est["tax"]))
            # Clear re_eval_date when a PAUSED item gets re-researched
            if safe_get(row, 0) == "PAUSED":
                updates.append((COL["re_eval_date"], recheck_date_str))
            write_row_partial(service, sheet_name, sheet_row, updates)

            # 3g. Listing copy — generate immediately for Tier 1/2 so it's ready before
            # Jordan reads the email. Tier 3 skips (PAUSED, not worth the API spend).
            seo_title_idx = col_to_idx(COL["seo_title"])
            if tier in (1, 2) and not safe_get(row, seo_title_idx):
                try:
                    copy_batch = [{
                        "title":         title,
                        "category":      category,
                        "cost":          costco_cost or "",
                        "sell_price":    ebay_price or "",
                        "site_url":      cat_config.get("site_url", ""),
                        "discount_code": business.get("discount_code", "SAVE10"),
                    }]
                    copy_result = generate_listing_copy(copy_batch)[0]
                    copy_updates = [
                        (COL["seo_title"],    copy_result.get("seo_title", "")),
                        (COL["bullets"],      copy_result.get("bullets", "")),
                        (COL["description"],  copy_result.get("description", "")),
                        (COL["redirect_msg"], copy_result.get("redirect_msg", "")),
                        (COL["meta_desc"],    copy_result.get("meta_desc", "")),
                        (COL["keywords"],     copy_result.get("keywords", "")),
                        (COL["alt_text"],     copy_result.get("alt_text", "")),
                    ]
                    write_row_partial(service, sheet_name, sheet_row, copy_updates)
                    logger.info(f"  Copy generated → {copy_result.get('seo_title','')[:60]}")
                except Exception as e:
                    logger.warning(f"  Listing copy failed: {e}")

            # Tier routing
            if tier == 1 and len(tier1_results) < 3:
                tier1_results.append({
                    "title": title, "row": sheet_row,
                    "score": weighted_score,
                    "lens_scores": result["lens_scores"],
                    "reasoning": reasoning,
                    "recommendation": result["recommendation"],
                    "costco_url": costco_url,
                    "stock": stock_status,
                    "demand_note": demand_note,
                })
            elif tier == 1 and len(tier1_results) >= 3:
                # Cap hit — demote to Tier 2
                tier = 2

            if tier == 2:
                stale_days = cat_config.get("stale_days", 30)
                tier2_recheck = (date.today() + timedelta(days=stale_days)).isoformat()
                new_tier2.append({
                    "title": title, "costco_url": costco_url,
                    "category": category,
                    "scored_date": date.today().isoformat(),
                    "recheck_date": tier2_recheck,
                    "score": weighted_score,
                    "reason": full_notes,
                })
            elif tier == 3:
                _append_tier3({
                    "title": title, "costco_url": costco_url,
                    "category": category,
                    "scored_date": date.today().isoformat(),
                    "score": weighted_score,
                    "reason": full_notes,
                })

            _update_category_performance(category, tier)
            researched_count += 1

            time.sleep(random.uniform(4, 6))   # pause between products — longer gap reduces Costco rate-limit hits

    # ── Step 4: Update Tier 2 watchlist ──────────────────────────
    tier2_watchlist = [
        item for item in tier2_watchlist
        if item.get("costco_url") not in tier2_recheck_urls
    ]
    tier2_watchlist.extend(new_tier2)
    _save_tier2_watchlist(tier2_watchlist)

    # ── Step 5: Tier 1 digest email ───────────────────────────────
    # Track data quality issues across the run (spot-fallback pricing used)
    spot_fallback_count = sum(
        1 for item in tier1_results + new_tier2
        if "melt" in str(item.get("reason", ""))
    )

    if tier1_results:
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        subject  = (
            f"[Tier 1] {len(tier1_results)} product(s) ready for your approval "
            f"— {date.today().isoformat()}"
        )
        lines = [
            f"{len(tier1_results)} product(s) scored Tier 1 from today's research run.",
            "Set status = APPROVED in your sheet to greenlight.\n",
        ]
        for i, item in enumerate(tier1_results, 1):
            lines.append(f"{i}. {item['title']} — Score: {item['score']}")
            lines.append(f"   Stock: {item['stock']}")
            for lens, text in item["reasoning"].items():
                lines.append(f"   {lens.replace('_', ' ').title()}: \"{text}\"")
            lines.append(f"   Recommendation: {item['recommendation']}")
            if item["demand_note"]:
                lines.append(f"   Watch: {item['demand_note']}")
            lines.append(f"   Sheet row: {item['row']}")
            lines.append(f"   Costco URL: {item['costco_url']}\n")
        lines.append(f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit")

        # Data quality notice — surfaces when eBay comp data was sparse
        if spot_fallback_count > 0:
            lines.append(
                f"\n--- DATA QUALITY NOTE ---\n"
                f"{spot_fallback_count} product(s) used melt-value pricing this run because "
                f"eBay sold comps returned no usable median price.\n"
                f"Suggested prices for those products are estimates (spot x 1.05), not market-validated.\n"
                f"Upgrade path: Terapeak (eBay Seller Hub > Research) provides 12 months of "
                f"historical sales data and would improve pricing accuracy for precious metals. "
                f"Integration is on the Phase 3 roadmap."
            )

        send_alert(subject, "\n".join(lines), urgent=False)
        logger.info(f"Tier 1 digest sent — {len(tier1_results)} product(s).")

    # ── Step 5b: Run summary (even with no Tier 1s) ───────────────
    # Send a brief summary so Jordan knows what ran even on quiet days
    elif to_research:
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        subject  = f"[WAT Research] {researched_count} scored — 0 Tier 1 — {date.today().isoformat()}"
        lines = [
            f"Research run complete. No Tier 1 products found today.",
            f"Scored: {researched_count} | Tier 2 (WATCH): {len(new_tier2)} | Tier 3 (PAUSED): {researched_count - len(tier1_results) - len(new_tier2)}",
        ]
        if spot_fallback_count > 0:
            lines.append(
                f"\nNote: {spot_fallback_count} product(s) used melt-value pricing (eBay comps sparse). "
                f"Terapeak integration would improve this — on Phase 3 roadmap."
            )
        lines.append(f"\nhttps://docs.google.com/spreadsheets/d/{sheet_id}/edit")
        send_alert(subject, "\n".join(lines), urgent=False)
        logger.info("Run summary sent (no Tier 1 results).")

    queued = len(to_research)
    skipped = queued - researched_count
    logger.info(
        f"Research run complete. "
        f"New products: {len(new_products)} | "
        f"Queued: {queued} | Researched: {researched_count}"
        + (f" | Skipped (session halt): {skipped}" if skipped else "") +
        f" | Tier 1: {len(tier1_results)} | "
        f"Tier 2: {len(new_tier2)} | "
        f"Watchlist: {len(tier2_watchlist)}"
    )

    # Refresh Summary dashboard so Jordan sees live counts immediately after research
    try:
        from tools.sheet_formatter import refresh_summary_tab
        fresh_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")
        refresh_summary_tab(service, sheet_name, all_data=fresh_data)
    except Exception as e:
        logger.warning(f"  Summary tab refresh failed (non-fatal): {e}")

    # Write structured results to the Run Log tab so Jordan can verify research ran
    from tools.run_logger import log_run_end as _log_run_end
    tier3_count = researched_count - len(tier1_results) - len(new_tier2)
    ebay_fail_note = ""
    if _ebay_fail_log:
        ebay_fail_note = f" | eBay failed ({len(_ebay_fail_log)}): {'; '.join(_ebay_fail_log[:5])}"
    _log_run_end("research", _run_start, {
        "status":       "error" if _ebay_fail_log and len(_ebay_fail_log) == researched_count else "ok",
        "new_products": len(new_products),
        "researched":   researched_count,
        "tier1":        len(tier1_results),
        "tier2":        len(new_tier2),
        "tier3":        max(0, tier3_count),
        "spot_gold":    _spot_prices.get("gold", ""),
        "spot_silver":  _spot_prices.get("silver", ""),
        "errors":       ebay_fail_note.strip(" |") if _ebay_fail_log else "",
        "notes":        f"Queued: {queued}" + (f" | Halted early: {skipped} skipped" if skipped else ""),
    }, service)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Costco product researcher")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max products to research per run (default: all)")
    def _positive_int(value):
        n = int(value)
        if n <= 0:
            raise argparse.ArgumentTypeError(f"--add-limit must be a positive integer, got {value!r}")
        return n
    parser.add_argument("--add-limit", type=_positive_int, default=None,
                        help="Max new products to add to sheet during discovery")
    parser.add_argument("--category", type=str, default=None,
                        help="Only research this category (e.g. 'Jewelry')")
    parser.add_argument("--discover-only", action="store_true",
                        help="Only discover new products, skip research scoring")
    parser.add_argument("--skip-discovery", action="store_true",
                        help="Skip Costco discovery, go straight to researching PENDING rows")
    args = parser.parse_args()
    run_researcher(limit=args.limit, add_limit=args.add_limit,
                   category_filter=args.category,
                   discover_only=args.discover_only,
                   skip_discovery=args.skip_discovery)
