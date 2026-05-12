"""
Tool: spot_price
Fetches live metal spot prices (gold, silver, platinum) via Yahoo Finance.
Ticker symbols: GC=F (gold futures), SI=F (silver futures), PL=F (platinum futures).

No API key required. Cached per run to avoid repeat calls.
Returns prices in USD per troy ounce.

Also provides check_spot_movement() — call once per daily sweep to alert when
gold/silver moves past a threshold since the previous run. A 1.5%+ move on
gold (~$45+ on a $3,000 bar) meaningfully changes margin calculations.
"""

import os
import time
import json
import urllib.request
from datetime import date
from loguru import logger

# Troy oz conversions
GRAMS_PER_TROY_OZ = 31.1035

# Cache: {metal: (price_usd_per_oz, fetched_at_unix)}
_CACHE: dict = {}
_CACHE_TTL = 3600  # 1 hour

_YAHOO_TICKERS = {
    "gold":     "GC%3DF",   # GC=F
    "silver":   "SI%3DF",   # SI=F
    "platinum": "PL%3DF",   # PL=F
}

_KARAT_PURITY = {
    24: 1.0,
    22: 22/24,
    18: 18/24,
    14: 14/24,
    10: 10/24,
}


def get_spot_price(metal: str = "gold") -> float | None:
    """
    Returns current spot price in USD per troy ounce.
    Uses Yahoo Finance quote API (free, no auth).
    Caches result for 1 hour.
    """
    metal = metal.lower()
    cached = _CACHE.get(metal)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]

    ticker = _YAHOO_TICKERS.get(metal)
    if not ticker:
        logger.warning(f"spot_price: unknown metal '{metal}'")
        return None

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&range=1d"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        price = (
            data["chart"]["result"][0]["meta"].get("regularMarketPrice")
            or data["chart"]["result"][0]["meta"].get("previousClose")
        )
        if price:
            _CACHE[metal] = (float(price), time.time())
            logger.info(f"  spot_price: {metal} = ${price:.2f}/oz")
            return float(price)
    except Exception as e:
        logger.warning(f"  spot_price fetch failed ({metal}): {e}")

    return None


def melt_value(weight_oz: float, metal: str = "gold", karat: int = 24) -> float | None:
    """
    Compute melt value in USD.
    weight_oz: weight in troy ounces
    karat: gold purity (24=pure, 22=.917, 18=.750, 14=.585, 10=.417)
    """
    spot = get_spot_price(metal)
    if spot is None or weight_oz <= 0:
        return None
    purity = _KARAT_PURITY.get(karat, 1.0)
    return round(spot * weight_oz * purity, 2)


def spot_premium_pct(sale_price: float, weight_oz: float,
                     metal: str = "gold", karat: int = 24) -> float | None:
    """
    Returns what % above melt value the sale price represents.
    e.g. spot_premium_pct(2200, 1.0, "gold") → ~5.2 if spot is $2090
    """
    mv = melt_value(weight_oz, metal, karat)
    if mv is None or mv <= 0:
        return None
    return round((sale_price - mv) / mv * 100, 1)


def check_spot_movement(
    gold_threshold_pct: float = 1.5,
    silver_threshold_pct: float = 2.0,
) -> dict | None:
    """
    Compare today's gold/silver prices against the last recorded prices.
    Returns an alert dict if any metal moved past its threshold, else None.

    Alert dict: {
        "metals": [{"metal": "gold", "prev": 3100.0, "curr": 3162.5, "pct": 2.02}],
        "summary": "Gold up 2.0% ($62.50/oz) since last check. ...",
        "urgent": True,   # True if move >= 2x threshold
    }

    Saves current prices to data/spot_history.json after each call.
    """
    history_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "spot_history.json"
    )

    # Load previous prices
    prev = {}
    if os.path.exists(history_path):
        try:
            with open(history_path) as f:
                prev = json.load(f)
        except Exception:
            prev = {}

    metals_to_check = {
        "gold":   (get_spot_price("gold"),   gold_threshold_pct),
        "silver": (get_spot_price("silver"), silver_threshold_pct),
    }

    alerts = []
    for metal, (curr_price, threshold) in metals_to_check.items():
        if curr_price is None:
            continue
        prev_entry = prev.get(metal, {})
        prev_price = prev_entry.get("price")
        prev_date  = prev_entry.get("date", "")

        if prev_price and prev_price > 0:
            pct_change = (curr_price - prev_price) / prev_price * 100
            abs_change = curr_price - prev_price
            if abs(pct_change) >= threshold:
                direction = "UP" if pct_change > 0 else "DOWN"
                alerts.append({
                    "metal":     metal,
                    "prev":      prev_price,
                    "curr":      curr_price,
                    "pct":       round(pct_change, 2),
                    "abs":       round(abs_change, 2),
                    "direction": direction,
                    "prev_date": prev_date,
                    "threshold": threshold,
                })

    # Save current prices
    today_str = date.today().isoformat()
    new_history = {}
    for metal, (curr_price, _) in metals_to_check.items():
        if curr_price:
            new_history[metal] = {"price": curr_price, "date": today_str}
    if new_history:
        try:
            with open(history_path, "w") as f:
                json.dump(new_history, f, indent=2)
        except Exception as e:
            logger.warning(f"spot_history save failed: {e}")

    if not alerts:
        return None

    # Build summary message
    lines = []
    for a in alerts:
        sign  = "+" if a["pct"] > 0 else ""
        lines.append(
            f"{a['metal'].title()} {a['direction']} {sign}{a['pct']:.1f}% "
            f"(${abs(a['abs']):.2f}/oz) since {a['prev_date']} "
            f"[${a['prev']:,.2f} → ${a['curr']:,.2f}]"
        )

    # Implications for inventory
    implications = []
    gold_alert = next((a for a in alerts if a["metal"] == "gold"), None)
    if gold_alert:
        if gold_alert["pct"] < 0:
            implications.append(
                "Gold dropped — WATCH products that were borderline may now clear the margin threshold. "
                "Consider triggering a Re-score Only research run."
            )
        else:
            implications.append(
                "Gold rose — ACTIVE listing margins may have tightened if Costco raised prices. "
                "Run Active Monitor to verify margin on any live listings."
            )

    summary = "\n".join(lines)
    if implications:
        summary += "\n\nAction suggested:\n" + "\n".join(implications)

    urgent = any(abs(a["pct"]) >= a["threshold"] * 2 for a in alerts)

    logger.info(f"  Spot movement alert: {summary[:120]}")
    return {
        "metals":  alerts,
        "summary": summary,
        "urgent":  urgent,
    }


def parse_gold_weight(title: str) -> tuple[float | None, int | None]:
    """
    Parse weight (troy oz) and karat from a product title.
    Returns (weight_oz, karat). Either may be None if not found.

    Examples:
      "1 oz Gold Bar PAMP Suisse"      → (1.0, 24)
      "5 Gram Pure Gold Framed"        → (0.1608, 24)
      "14kt Yellow Gold Rope Bracelet" → (None, 14)
      "22kt Yellow Gold Cuff Bangle"   → (None, 22)
      "Round Brilliant 1.30 ctw"       → (None, None)  — diamonds, skip
    """
    import re
    title_lower = title.lower()

    weight_oz = None
    karat = None

    # Troy oz: "1 oz", "1/4 oz", "1/2 oz", "0.5 oz"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:troy\s*)?oz", title_lower)
    if m:
        weight_oz = float(m.group(1))
    else:
        # Fractional oz: "1/4 oz", "1/2 oz", "1/10 oz"
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*oz", title_lower)
        if m:
            weight_oz = int(m.group(1)) / int(m.group(2))

    # Grams: "5 gram", "5 g", "1 gram"
    if weight_oz is None:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:gram|grams|g\b)", title_lower)
        if m:
            weight_oz = round(float(m.group(1)) / GRAMS_PER_TROY_OZ, 6)

    # Karat: "14kt", "18k", "22 karat", "24k"
    m = re.search(r"(\d{1,2})\s*(?:kt|k|karat)", title_lower)
    if m:
        k = int(m.group(1))
        if k in _KARAT_PURITY:
            karat = k

    # Pure gold bars default to 24k
    if karat is None and weight_oz is not None and "pure gold" in title_lower:
        karat = 24

    return weight_oz, karat
