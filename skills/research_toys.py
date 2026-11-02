"""
Skill: research_toys
3-pass research for Costco toys (LEGO, Magna-Tiles, licensed sets) — EXPERIMENTAL Q4 category.

Extends: C:\\Users\\jorda\\.claude\\skills\\base_research.ResearchBase
Project: reselling-agent

Toys only resell during the holiday rush (Oct–Dec, tailing into January), so this skill scores
conservatively and adds deterministic signals the generic dimensions don't capture:
  - Brand/trend: LEGO / Magna-Tiles = premium, licensed (Star Wars, Pokémon, Bluey …) = strong,
    mainstream toy makers = neutral, anything else = off-brand → skipped
  - Sellout: "sold out on Amazon / Walmart" in the research text → eBay premium (demand boost)
  - Q4 seasonality: demand is capped outside the season, and Tier 1 needs an in-season month
  - Margin: Tier 1 needs the FULL col I net (compute_net) ≥ TOYS_TIER1_MIN_NET
  - Shipping size: bulky items (ride-ons, playhouses, > SIZE_BULKY_IN inches) score low on
    fulfillment and are skipped unless the net clears BULKY_MIN_NET

Hard filters (always Tier 3): commodity toys, off-brand toys, bulky low-margin items.

Pass 3 (multi-lens scoring) is inherited from ResearchBase. researcher.py calls
adjust_dimensions() before Pass 3 and toy_gate() last, so the gate has the final word.
"""

import os
import re
import sys
from datetime import date

# Try global skills dir (Windows local dev), fall back to bundled copy in skills/ (CI/Ubuntu)
_GLOBAL_SKILLS = r"C:\Users\jorda\.claude\skills"
_LOCAL_SKILLS  = os.path.dirname(os.path.abspath(__file__))
for _p in [_GLOBAL_SKILLS, _LOCAL_SKILLS]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from base_research import ResearchBase


# ── Thresholds (conservative on purpose — this is a short experiment) ─────────
TOYS_TIER1_MIN_NET = 8.00     # full col I net $/unit needed for Tier 1 (SCORED)
BULKY_MIN_NET      = 25.00    # a bulky toy is skipped unless its net clears this
SIZE_BULKY_IN      = 36       # any side longer than this (inches) = bulky
SIZE_SUM_BULKY_IN  = 70       # length + width + height above this = bulky
SIZE_SUM_LARGE_IN  = 45       # above this = large (lower fulfillment score, not skipped)
SEASON_MONTHS      = (10, 11, 12, 1)   # Oct–Jan: Tier 1 allowed
OFF_SEASON_DEMAND_CAP = 5.0

# ── Brand tiers (regex fragments, matched word-bounded on title + Costco brand) ──
PREMIUM_BRANDS = [r"lego", r"magna-?tiles", r"playmobil", r"hot wheels", r"barbie", r"nerf",
                  r"squishmallows?", r"pok[eé]mon", r"jellycat", r"connetix", r"picasso ?tiles"]
LICENSED = [r"star wars", r"marvel", r"avengers", r"spider-?man", r"disney", r"frozen", r"pixar",
            r"harry potter", r"paw patrol", r"bluey", r"minecraft", r"super mario", r"nintendo",
            r"sonic", r"fortnite", r"jurassic", r"transformers", r"batman", r"dc comics",
            r"peppa pig", r"cocomelon", r"barbie", r"gabby'?s dollhouse"]
MAINSTREAM_BRANDS = [r"hasbro", r"mattel", r"fisher-?price", r"melissa (&|and) doug", r"vtech",
                     r"little tikes", r"ravensburger", r"crayola", r"kidkraft", r"step2", r"hape",
                     r"lol surprise", r"l\.o\.l\.", r"spin master", r"funko", r"play-?doh",
                     r"monopoly", r"hatchimals", r"leapfrog", r"radio flyer", r"sphero",
                     r"snap circuits", r"thames (&|and) kosmos", r"national geographic",
                     r"schleich", r"brio", r"tonka", r"matchbox", r"uno", r"jenga", r"catan"]

COMMODITY = [r"crayons?", r"coloring books?", r"bubbles?", r"balloons?", r"sidewalk chalk",
             r"pool (floats?|toys?|noodles?)", r"party favors?", r"stocking stuffers?",
             r"variety pack", r"assorted", r"bulk", r"sticker (books?|packs?)", r"glow sticks?",
             r"water (guns?|balloons?)", r"bath toys?", r"slime"]
BULKY = [r"playhouse", r"ride-?ons?", r"power wheels", r"trampoline", r"swing set", r"play kitchen",
         r"basketball (hoop|system)", r"climbers?", r"slide", r"sandbox", r"go-?kart",
         r"wagon", r"rocking horse", r"table (&|and) chairs?", r"(kids'?|electric|12v|24v) (car|truck|jeep|atv)",
         r"teepee", r"ball pit", r"play ?yard", r"train table"]

SELLOUT = [r"sold out", r"out of stock (on|at) (amazon|walmart|target)",
           r"(amazon|walmart|target)\W+(is |are )?(sold out|out of stock)", r"hard to find",
           r"selling out", r"back-?ordered", r"scalp(ed|ers?|ing)", r"resale premium"]


def _rx(frags):
    return re.compile(r"\b(?:" + "|".join(frags) + r")\b", re.IGNORECASE)


_PREMIUM_RX, _LICENSED_RX, _MAINSTREAM_RX = _rx(PREMIUM_BRANDS), _rx(LICENSED), _rx(MAINSTREAM_BRANDS)
_COMMODITY_RX, _BULKY_RX, _SELLOUT_RX = _rx(COMMODITY), _rx(BULKY), _rx(SELLOUT)


# ── Signals ───────────────────────────────────────────────────────────────────

def brand_tier(title, brand=None):
    """'premium' | 'licensed' | 'mainstream' | 'off_brand' from the title + Costco's brand field."""
    text = f"{title or ''} {brand or ''}"
    if _PREMIUM_RX.search(text):
        return "premium"
    if _LICENSED_RX.search(text):
        return "licensed"
    if _MAINSTREAM_RX.search(text):
        return "mainstream"
    return "off_brand"


BRAND_SCORES = {"premium": 9, "licensed": 8, "mainstream": 6, "off_brand": 2}


def is_commodity(title):
    return bool(_COMMODITY_RX.search(str(title or "")))


def size_class(title, dimensions=None):
    """'bulky' | 'large' | 'standard'. Title keywords win; else Costco's dimensions (inches)."""
    if _BULKY_RX.search(str(title or "")):
        return "bulky"
    dims = []
    for k in ("length", "width", "height"):
        try:
            dims.append(float((dimensions or {}).get(k) or 0))
        except (TypeError, ValueError):
            dims.append(0.0)
    if max(dims) > SIZE_BULKY_IN or sum(dims) > SIZE_SUM_BULKY_IN:
        return "bulky"
    if sum(dims) > SIZE_SUM_LARGE_IN:
        return "large"
    return "standard"


SHIP_SCORES = {"bulky": 2, "large": 5, "standard": 8}


def sellout_signal(text):
    """True when the research text says it's sold out at the big retailers (eBay premium), else None
    (unknown — never assume it isn't)."""
    return True if _SELLOUT_RX.search(str(text or "")) else None


def season_score(month):
    """0-10 holiday relevance for a calendar month."""
    return {10: 8, 11: 10, 12: 9, 1: 4}.get(int(month), 2)


def in_season(month):
    return int(month) in SEASON_MONTHS


def toy_signals(title, brand=None, dimensions=None, text="", today=None):
    """All toy signals in one dict (also feeds the col AV note line)."""
    month = (today or date.today()).month
    tier = brand_tier(title, brand)
    size = size_class(title, dimensions)
    return {
        "brand_tier": tier,
        "brand_score": BRAND_SCORES[tier],
        "commodity": is_commodity(title),
        "size": size,
        "ship_score": SHIP_SCORES[size],
        "sellout": sellout_signal(text),
        "season_score": season_score(month),
        "in_season": in_season(month),
    }


def format_signals(sig):
    sellout = "sold out at retail" if sig["sellout"] else "unknown"
    season = "in season" if sig["in_season"] else "off season"
    return (f"Toys (experimental): brand {sig['brand_tier'].replace('_', '-')} · sellout {sellout} · "
            f"{season} · ship {sig['size']}")


def adjust_dimensions(dimension_scores, sig):
    """Blend the toy signals into the generic dimensions (mutates + returns dimension_scores).
    demand = 50% research + 25% brand + 25% seasonality, +1.5 on a confirmed sellout, capped off
    season; fulfillment_risk can only go DOWN for a large/bulky box."""
    base = float(dimension_scores.get("demand_signals", 5) or 0)
    demand = 0.5 * base + 0.25 * sig["brand_score"] + 0.25 * sig["season_score"]
    if sig["sellout"]:
        demand += 1.5
    if not sig["in_season"]:
        demand = min(demand, OFF_SEASON_DEMAND_CAP)
    dimension_scores["demand_signals"] = round(min(10.0, demand), 1)
    current_ship = dimension_scores.get("fulfillment_risk")
    dimension_scores["fulfillment_risk"] = (sig["ship_score"] if current_ship is None
                                            else min(float(current_ship), sig["ship_score"]))
    return dimension_scores


def skip_reason(sig, real_net):
    """Hard filters → a reason string (the row goes to Tier 3), or None."""
    if sig["commodity"]:
        return "commodity toy (low resale value)"
    if sig["brand_tier"] == "off_brand":
        return "off-brand toy (no brand or license premium)"
    if sig["size"] == "bulky" and (real_net is None or real_net < BULKY_MIN_NET):
        net = "unknown" if real_net is None else f"${real_net:.2f}"
        return f"bulky to ship, net {net} < ${BULKY_MIN_NET:.0f}"
    return None


def toy_gate(tier, sig, real_net):
    """Final tier for a toy + a note ('' when unchanged). Runs AFTER every other override
    (monthly-profit, sale fast-track) so the hard filters can't be promoted past."""
    reason = skip_reason(sig, real_net)
    if reason:
        return 3, f"\nToys hard filter: {reason} → skipped."
    if tier == 1:
        if real_net is None or real_net < TOYS_TIER1_MIN_NET:
            net = "unknown" if real_net is None else f"${real_net:.2f}"
            return 2, f"\nToys gate: net {net} < ${TOYS_TIER1_MIN_NET:.2f} → WATCH."
        if not sig["in_season"]:
            return 2, "\nToys gate: off season (Oct–Jan only) → WATCH."
    return tier, ""


class ToysResearch(ResearchBase):
    CATEGORY_NAME    = "Toys"
    FULFILLMENT_RISK = "medium"   # most boxes are light; bulky ones are filtered out

    PASS1_SIGNALS = [
        "eBay sold listings — search the LEGO set number / exact product name first",
        "Is it sold out on Amazon / Walmart / Target? Sellouts are what make toy resale work",
        "Brand/license: LEGO, Magna-Tiles, Pokémon, Star Wars, Bluey … vs off-brand (skip)",
        "Holiday trend: 'hot toys' lists, TikTok/YouTube buzz for this item this season",
        "Costco-exclusive bundle or set number? Exclusives have no direct retail comp",
        "Box size/weight — oversized boxes (ride-ons, playhouses) eat the margin in shipping",
    ]

    PASS2_NOTES = """
For toys, also factor:
- The resale window is Oct–Dec; after January demand collapses. Price for the holiday, score cautiously.
- Retired / Costco-exclusive LEGO sets and sold-out licensed items carry a real eBay premium.
- Mainstream items in stock everywhere sell at or below retail on eBay — margin is the whole story.
- Ship-by dates: eBay buyers want delivery before Dec 25; drop-ship lead time matters mid-December.
"""

    def run_pass1(self, product_title, category, costco_cost):
        result = super().run_pass1(product_title, category, costco_cost)
        result["sellout_check"] = ("Check Amazon/Walmart/Target stock. Sold out at retail = eBay premium; "
                                   "in stock everywhere = commodity pricing.")
        return result

    def run_pass2(self, search_terms, costco_cost, fee_rate):
        result = super().run_pass2(search_terms, costco_cost, fee_rate)
        result["toys_notes"] = self.PASS2_NOTES
        result["suggested_searches"] = [f"{t} sold ebay" for t in search_terms]
        return result


# ── Module-level entry points ─────────────────────────────────────────────────

_toys = ToysResearch()

def run_pass3(dimension_scores, category_config, reasoning=None):
    return _toys.run_pass3(dimension_scores, category_config, reasoning)

toys_pass3 = run_pass3
