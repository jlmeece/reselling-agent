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
from skills.scoring import score_dimension


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
            COL["notes"]:        "Discovered by agent — awaiting research",
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
            f"  Gold spot price: ${spot_data['spot_price']:.2f}/oz\n"
            f"  Product weight: {spot_data['weight_oz']:.4f} troy oz\n"
            f"  Karat/purity: {spot_data['karat']}kt\n"
            f"  Melt value: ${spot_data['melt_value']:.2f}\n"
            f"  eBay premium above melt: {spot_data['premium_pct']:+.1f}%\n"
            f"  Note: buyers pay premium for Costco trust, mint brand, and convenience — not just melt value.\n"
            f"  IMPORTANT: Sold 90-day median may be stale (reflects older gold prices). Use median active listing price as the primary eBay price signal for gold bars.\n\n"
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


def _suggest_ebay_price(costco_cost, ebay_data: dict, fee_rate: float) -> float | None:
    """
    Data-backed eBay listing price suggestion.
    Anchor: median sold price (or median active as fallback).
    Saturation discount: -3% when active listings > 2× sold count (crowded market).
    Margin floor: price must yield >= 10% net margin after fees.
    Returns price rounded to nearest .99, or None if data is insufficient.
    """
    MIN_MARGIN = 0.10

    anchor = ebay_data.get("median_sold") or ebay_data.get("median_active")
    if not anchor:
        return None

    price = float(anchor)

    sold   = ebay_data.get("sold_90d") or 0
    active = ebay_data.get("active_count") or 0
    if sold > 0 and active > sold * 2:
        price *= 0.97  # crowded market — price 3% below median to move faster

    try:
        cost = float(str(costco_cost).replace("$", "").replace(",", ""))
        if cost > 0:
            min_price = cost / (1 - fee_rate - MIN_MARGIN)
            if price < min_price:
                return None  # market price can't cover costs — not viable, don't suggest
    except (ValueError, TypeError):
        pass

    price = round(price) - 0.01
    return price if price >= 1 else None


def _pass3_for_category(category):
    if category.lower() in ("jewelry", "gold"):
        return gold_pass3
    if category.lower() == "watches":
        return watches_pass3
    return outdoor_pass3


# ── Main research loop ────────────────────────────────────────────

def run_researcher(limit=None, category_filter=None, discover_only=False, skip_discovery=False):
    """
    limit:            max products to score this run (None = all PENDING)
    category_filter:  only research this category (e.g. 'Jewelry')
    discover_only:    add new products to sheet but skip scoring
    skip_discovery:   skip Costco scraping, go straight to researching PENDING rows
    """
    config   = _load_config()
    COL      = _load_col_map()
    business = config["business"]
    categories = config["categories"]

    logger.info(f"Researcher started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    service    = get_sheets_service()
    sheet_name = business["sheet_name"]
    start_row  = business["data_start_row"]
    end_row    = business["data_end_row"]

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AZ{end_row}")

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

        # Add new products to sheet as PENDING (single batch API call)
        if new_products:
            _add_new_products_batch(service, sheet_name, new_products, COL)

        if discover_only:
            logger.info(f"Discover-only mode — {len(new_products)} products added. Skipping research.")
            return

        # Reload sheet data now that new rows are added
        all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AZ{end_row}")

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

    with make_browser() as costco_page:
        for _product_idx, (sheet_row, row) in enumerate(to_research):
            # Refresh Costco session every 20 products to prevent cookie expiry
            if _product_idx > 0 and _product_idx % 20 == 0:
                refresh_session(costco_page)

            title        = safe_get(row, 2)   # col C
            category     = safe_get(row, 3)   # col D
            costco_url   = safe_get(row, 17)  # col R
            costco_cost  = safe_get(row, 6)   # col G
            ebay_price   = safe_get(row, 7)   # col H

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
            if live_price and not costco_cost:
                costco_cost = live_price

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
                    metal = "gold"
                    k = karat or 24
                    mv = melt_value(w_oz, metal, k)
                    ebay_median = None  # filled after eBay comps
                    spot_data = {
                        "spot_price": _spot_prices["gold"],
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
                    (COL["notes"],         "Warehouse-only item — not available for online dropship"),
                ])
                continue

            # 3a-ii. Cart estimate (tax + Costco shipping) — only if in stock
            cart_est: dict = {}
            if in_stock:
                try:
                    cart_est = get_cart_estimate(costco_url, page=costco_page)
                except Exception as e:
                    logger.warning(f"  Cart estimate failed: {e}")

            time.sleep(1)

            # 3b. eBay comp research (reuse CDP Chrome — same session, avoids bot detection)
            try:
                ebay_data = get_ebay_comps(
                    title, category, page=costco_page,
                    brand=brand, model=model,
                    ebay_category_id=cat_config.get("ebay_category_id"),
                )
            except Exception as e:
                logger.warning(f"  eBay research failed: {e}")
                ebay_data = {"sold_90d": None, "avg_sold_price": None,
                             "active_count": None, "fee_rate": fee_rate,
                             "query_used": "", "query_strategy": "error", "note": str(e)}

            # Fill eBay premium into spot_data now that we have comps
            if spot_data and spot_data.get("melt_value"):
                ebay_ref = ebay_data.get("median_sold") or ebay_data.get("median_active")
                if ebay_ref:
                    spot_data["premium_pct"] = spot_premium_pct(
                        ebay_ref, spot_data["weight_oz"],
                        karat=spot_data["karat"]
                    )

            # Suggested listing price — fill col H when not already set
            suggested_price = _suggest_ebay_price(costco_cost, ebay_data, fee_rate)

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
                    (COL["notes"], f"Research failed: {e}"),
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

            # ── Summary header line (always the first thing Jordan sees in Col T) ──
            # Shows tier, score, suggested price, estimated margin, and clickable Costco URL.
            margin_str = ""
            if suggested_price and costco_cost:
                try:
                    cost_f = float(str(costco_cost).replace("$", "").replace(",", ""))
                    net_f  = suggested_price - cost_f - suggested_price * fee_rate
                    margin_str = f" | ~{net_f / suggested_price * 100:.1f}% margin"
                except (ValueError, TypeError):
                    pass
            price_summary = f"Sugg: ${suggested_price:,.2f}{margin_str} | " if suggested_price else ""
            summary_line = f"[T{tier} | Score {weighted_score} | {price_summary}Costco: {costco_url}]"

            notes = (
                f"{summary_line}\n"
                f"Tier {tier} (score {weighted_score}) | {result['recommendation']} | "
                f"Strongest lens: {top_lens.replace('_', ' ').title()} | {demand_note}"
                f"{price_note}\n"
                f"Community: {comm_summary}\n"
                f"Sources: {comm_breakdown}"
            )
            if intent_seen:
                notes += f"\nIntent phrases: " + " | ".join(intent_seen[:3])
            if spot_data and spot_data.get("melt_value"):
                prem = f"{spot_data['premium_pct']:+.1f}%" if spot_data.get("premium_pct") is not None else "unknown"
                notes += (
                    f"\nGold spot: ${spot_data['spot_price']:.2f}/oz | "
                    f"Weight: {spot_data['weight_oz']:.4f} oz | "
                    f"Melt: ${spot_data['melt_value']:.2f} | "
                    f"eBay premium above melt: {prem}"
                )
            if purchase_limit:
                notes += f"\nPurchase limit: {purchase_limit}/day — list max {purchase_limit} units on eBay"
            if cart_est:
                ship_str = f"${cart_est['shipping']:.2f}" if cart_est.get("shipping") is not None else "?"
                tax_str  = f"${cart_est['tax']:.2f}" if cart_est.get("tax") is not None else "?"
                notes += f"\nCostco cart est: ship={ship_str} tax={tax_str}"
                if cart_est.get("delivery_window"):
                    notes += f" | delivery: {cart_est['delivery_window']}"

            # 3f. Write to sheet
            from datetime import timedelta
            recheck_date_str = (date.today() + timedelta(days=30)).isoformat()
            updates = [
                (COL["demand_score"],  weighted_score),
                (COL["stock_status"],  stock_status),
                (COL["last_checked"],  datetime.now().strftime("%Y-%m-%d %H:%M")),
                (COL["notes"],         notes),
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
            # Cart estimate — ship_cost overwrites col X; tax overwrites formula in col Z
            if cart_est.get("shipping") is not None:
                updates.append((COL["ship_cost"], cart_est["shipping"]))
            if cart_est.get("tax") is not None:
                updates.append((COL["tax_est"], cart_est["tax"]))
            # purchase_limit is captured in notes text; sheet col AO not yet provisioned
            # Clear re_eval_date when a PAUSED item gets re-researched
            if safe_get(row, 0) == "PAUSED":
                updates.append((COL["re_eval_date"], recheck_date_str))
            write_row_partial(service, sheet_name, sheet_row, updates)

            # 3g. Listing copy — generate immediately for Tier 1/2 so it's ready before
            # Jordan reads the email. Tier 3 skips (PAUSED, not worth the API spend).
            if tier in (1, 2) and not safe_get(row, 28):  # col AC (index 28) = seo_title
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
                new_tier2.append({
                    "title": title, "costco_url": costco_url,
                    "category": category,
                    "scored_date": date.today().isoformat(),
                    "recheck_date": result.get("recheck_date", ""),
                    "score": weighted_score,
                    "reason": notes,
                })
            elif tier == 3:
                _append_tier3({
                    "title": title, "costco_url": costco_url,
                    "category": category,
                    "scored_date": date.today().isoformat(),
                    "score": weighted_score,
                    "reason": notes,
                })

            _update_category_performance(category, tier)

            time.sleep(2)   # pause between products

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
        subject  = f"[WAT Research] {len(to_research)} scored — 0 Tier 1 — {date.today().isoformat()}"
        lines = [
            f"Research run complete. No Tier 1 products found today.",
            f"Scored: {len(to_research)} | Tier 2 (WATCH): {len(new_tier2)} | Tier 3 (PAUSED): {len(to_research) - len(tier1_results) - len(new_tier2)}",
        ]
        if spot_fallback_count > 0:
            lines.append(
                f"\nNote: {spot_fallback_count} product(s) used melt-value pricing (eBay comps sparse). "
                f"Terapeak integration would improve this — on Phase 3 roadmap."
            )
        lines.append(f"\nhttps://docs.google.com/spreadsheets/d/{sheet_id}/edit")
        send_alert(subject, "\n".join(lines), urgent=False)
        logger.info("Run summary sent (no Tier 1 results).")

    logger.info(
        f"Research run complete. "
        f"New products: {len(new_products)} | "
        f"Researched: {len(to_research)} | "
        f"Tier 1: {len(tier1_results)} | "
        f"Tier 2: {len(new_tier2)} | "
        f"Watchlist: {len(tier2_watchlist)}"
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Costco product researcher")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max products to research per run (default: all)")
    parser.add_argument("--category", type=str, default=None,
                        help="Only research this category (e.g. 'Jewelry')")
    parser.add_argument("--discover-only", action="store_true",
                        help="Only discover new products, skip research scoring")
    parser.add_argument("--skip-discovery", action="store_true",
                        help="Skip Costco discovery, go straight to researching PENDING rows")
    args = parser.parse_args()
    run_researcher(limit=args.limit, category_filter=args.category,
                   discover_only=args.discover_only,
                   skip_discovery=args.skip_discovery)
