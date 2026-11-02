"""skills/research_toys.py — the experimental Q4 Toys skill: signals, hard filters, the tier gate,
and its wiring into run_researcher (every outside call stubbed, as in test_research_targeted)."""
import contextlib
import os
import sys
from datetime import date

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import research_toys as T  # noqa: E402

NOV = date(2026, 11, 15)
MAR = date(2027, 3, 15)
DIMS = {"length": 15, "width": 10, "height": 4}


def _sig(title, brand=None, dims=DIMS, text="", today=NOV):
    return T.toy_signals(title, brand, dims, text, today=today)


# ── signals ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,brand,tier", [
    ("LEGO Star Wars Millennium Falcon 75375", None, "premium"),
    ("Magna-Tiles Clear Colors 100-piece Set", None, "premium"),
    ("Bluey Ultimate Playset", None, "licensed"),
    ("Kids' Building Set 500 pieces", "Mattel", "mainstream"),
    ("Ravensburger 1000-piece Puzzle", None, "mainstream"),
    ("Kids' Building Set 500 pieces", None, "off_brand"),
])
def test_brand_tier(title, brand, tier):
    assert T.brand_tier(title, brand) == tier


def test_size_class_from_keywords_and_dimensions():
    assert T.size_class("Little Tikes Outdoor Playhouse", DIMS) == "bulky"
    assert T.size_class("LEGO Technic set", {"length": 40, "width": 10, "height": 5}) == "bulky"
    assert T.size_class("LEGO Technic set", {"length": 25, "width": 18, "height": 10}) == "large"
    assert T.size_class("LEGO Technic set", DIMS) == "standard"
    assert T.size_class("LEGO Technic set", None) == "standard"          # no dims = not penalized
    assert T.size_class("LEGO set", {"length": "n/a"}) == "standard"


def test_sellout_signal_is_true_or_unknown_never_false():
    assert T.sellout_signal("This set is sold out on Amazon and Walmart") is True
    assert T.sellout_signal("Walmart is out of stock, scalpers listing at 2x") is True
    assert T.sellout_signal("widely available everywhere") is None
    assert T.sellout_signal(None) is None


def test_season_scores_peak_in_november_and_drop_after_january():
    assert T.season_score(11) == 10
    assert T.season_score(10) > T.season_score(1) > T.season_score(3)
    assert T.in_season(1) and not T.in_season(2) and not T.in_season(9)


def test_commodity_detection():
    assert T.is_commodity("Crayola Crayons 120-count")
    assert T.is_commodity("Assorted Bath Toys 12-pack")
    assert not T.is_commodity("LEGO Icons Botanical Bouquet")


# ── dimension adjustment ──────────────────────────────────────────────────────

def test_adjust_dimensions_premium_sellout_in_season_raises_demand():
    dims = {"demand_signals": 6, "fulfillment_risk": 7}
    T.adjust_dimensions(dims, _sig("LEGO Star Wars set", text="sold out on Amazon"))
    assert dims["demand_signals"] == round(0.5 * 6 + 0.25 * 9 + 0.25 * 10 + 1.5, 1)
    assert dims["fulfillment_risk"] == 7                 # standard box never raises it


def test_adjust_dimensions_off_season_caps_demand_and_bulky_lowers_fulfillment():
    dims = {"demand_signals": 10, "fulfillment_risk": 8}
    T.adjust_dimensions(dims, _sig("LEGO playhouse", text="sold out", today=MAR))
    assert dims["demand_signals"] == T.OFF_SEASON_DEMAND_CAP
    assert dims["fulfillment_risk"] == 2


# ── hard filters + gate ───────────────────────────────────────────────────────

def test_gate_skips_commodity_even_from_tier1():
    tier, note = T.toy_gate(1, _sig("LEGO Party Favors assorted"), 30.0)
    assert tier == 3 and "commodity" in note


def test_gate_skips_off_brand():
    tier, note = T.toy_gate(1, _sig("Kids' Magnetic Building Blocks 60pc"), 30.0)
    assert tier == 3 and "off-brand" in note


@pytest.mark.parametrize("net,tier_out", [(None, 3), (10.0, 3), (24.99, 3), (40.0, 1)])
def test_gate_skips_bulky_unless_net_clears_bulky_floor(net, tier_out):
    tier, _ = T.toy_gate(1, _sig("Little Tikes Cozy Coupe Ride-On", brand="Little Tikes"), net)
    assert tier == tier_out


def test_gate_tier1_needs_min_net():
    tier, note = T.toy_gate(1, _sig("LEGO Icons set"), 5.0)
    assert tier == 2 and "WATCH" in note
    assert T.toy_gate(1, _sig("LEGO Icons set"), None)[0] == 2
    assert T.toy_gate(1, _sig("LEGO Icons set"), T.TOYS_TIER1_MIN_NET) == (1, "")


def test_gate_tier1_off_season_becomes_watch():
    tier, note = T.toy_gate(1, _sig("LEGO Icons set", today=MAR), 50.0)
    assert tier == 2 and "off season" in note


def test_gate_never_promotes():
    assert T.toy_gate(2, _sig("LEGO Icons set"), 50.0) == (2, "")
    assert T.toy_gate(3, _sig("LEGO Icons set"), 50.0) == (3, "")


def test_run_pass3_returns_a_tier():
    out = T.run_pass3({"margin_potential": 8, "demand_signals": 8, "competition_density": 6,
                       "costco_availability": 10, "fulfillment_risk": 8}, {"seasonal_peak_months": [11]})
    assert out["tier"] in (1, 2, 3) and "lens_scores" in out


# ── config ────────────────────────────────────────────────────────────────────

def _config():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "categories.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_config_toys_category_is_capped_and_classified():
    from tools.costco_savings import classify_category, compile_keywords
    cfg = _config()
    toys = cfg["categories"]["Toys"]
    assert toys["research_skill"] == "skills/research_toys.py"
    assert toys["max_discovery"] <= 20 and toys["max_active"] <= 10
    kw = compile_keywords(cfg["business"]["savings"]["keywords"])
    paths = compile_keywords(cfg["business"]["savings"]["paths"])
    assert classify_category("LEGO Star Wars Millennium Falcon", kw) == "Toys"
    assert classify_category("Jellycat Plush Bunny", kw) == "Toys"
    assert classify_category("Plush Throw Blanket", kw) is None
    assert classify_category("KitchenAid Stand Mixer", kw) == "Small Appliances"   # other categories win
    assert classify_category("Toys > Building Sets & Blocks", paths) == "Toys"
    assert classify_category("Pet Supplies > Pet Toys & Treats", paths) is None


# ── researcher wiring ─────────────────────────────────────────────────────────

import agents.researcher as R  # noqa: E402

COL = R._load_col_map()


def _row(title):
    r = [""] * 48
    r[R.col_to_idx(COL["status"])] = "PENDING"
    r[R.col_to_idx(COL["title"])] = title
    r[R.col_to_idx(COL["category"])] = "Toys"
    r[R.col_to_idx(COL["costco_cost"])] = "60"
    r[R.col_to_idx(COL["ebay_price"])] = "120"
    r[R.col_to_idx(COL["costco_url"])] = "https://www.costco.com/.product.111.html"
    return r


@pytest.fixture
def research(monkeypatch):
    calls = {"writes": []}

    def go(title, brand="LEGO"):
        rows = [_row(title)]
        monkeypatch.setattr(R, "get_sheets_service", lambda: "svc")
        monkeypatch.setattr(R, "ensure_grid_columns", lambda *a, **k: None)
        monkeypatch.setattr(R, "read_sheet", lambda svc, rng: rows)
        monkeypatch.setattr(R, "make_browser", lambda: contextlib.nullcontext("page"))
        monkeypatch.setattr(R, "refresh_session", lambda page: None)
        monkeypatch.setattr(R, "scrape_costco", lambda url, page=None: {
            "stock_status": "In Stock", "price": 59.99, "brand": brand, "model": "",
            "in_stock": True, "dimensions": DIMS, "image_urls": []})
        monkeypatch.setattr(R, "get_cart_estimate", lambda url, page=None: {})
        monkeypatch.setattr(R, "get_ebay_comps", lambda *a, **k: {
            "sold_90d": 40, "avg_sold_price": 120.0, "active_count": 12, "median_sold": 120.0,
            "lowest_active": 115.0, "price_basis": "median", "query_strategy": "title"})
        monkeypatch.setattr(R, "get_community_signals", lambda *a, **k: {
            "signal_strength": 3, "summary": "quiet", "source_breakdown": {}, "intent_phrases": []})
        monkeypatch.setattr(R, "_run_claude_research", lambda *a, **k: {
            "dimension_scores": {"margin_potential": 8, "demand_signals": 8, "competition_density": 6,
                                 "fulfillment_risk": 8},
            "reasoning": {k: "" for k in ("conservative_analyst", "growth_operator", "brand_builder",
                                          "volume_flipper")},
            "demand_note": "sold out on Amazon"})
        monkeypatch.setattr(R, "write_row_partial",
                            lambda svc, sheet, row, pairs: calls["writes"].append((row, dict(pairs))))
        monkeypatch.setattr(R, "seed_formula_row", lambda *a, **k: None)
        monkeypatch.setattr(R, "log_sale", lambda *a, **k: None)
        monkeypatch.setattr(R, "generate_listing_copy", lambda batch: [{} for _ in batch])
        monkeypatch.setattr(R, "send_alert", lambda *a, **k: None)
        monkeypatch.setattr(R, "_load_tier2_watchlist", lambda: [])
        monkeypatch.setattr(R, "_save_tier2_watchlist", lambda items: None)
        monkeypatch.setattr(R, "_append_tier3", lambda e: None)
        monkeypatch.setattr(R, "_update_category_performance", lambda c, t: None)
        monkeypatch.setattr(R, "rank_rows_by_sharpe", lambda rows: [])
        monkeypatch.setattr(R.time, "sleep", lambda s: None)
        import tools.run_logger as rl
        import tools.sheet_formatter as sf
        monkeypatch.setattr(rl, "log_run_end", lambda *a, **k: None)
        monkeypatch.setattr(sf, "refresh_summary_tab", lambda *a, **k: None)
        R.run_researcher(only_rows=[4], notify=False)
        return next(w for r, w in calls["writes"] if r == 4 and COL["full_notes"] in w)
    return go


def test_researcher_skips_off_brand_toy(research):
    w = research("Kids' Building Blocks 300 pieces", brand="")
    assert w[COL["status"]] == "PAUSED_DEMAND"
    assert "Toys hard filter: off-brand" in w[COL["full_notes"]]


def test_researcher_notes_toy_signals_for_branded_toy(research):
    w = research("LEGO Star Wars X-Wing 75355")
    assert "Toys (experimental): brand premium · sellout sold out at retail" in w[COL["full_notes"]]
    assert "Toys hard filter" not in w[COL["full_notes"]]
