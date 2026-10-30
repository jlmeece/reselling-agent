"""run_researcher targeted repair mode (only_rows / keep_status_rows / force_copy /
copy_overrides / notify) — every outside call is stubbed; asserts what gets written."""
import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.researcher as R  # noqa: E402

COL = R._load_col_map()


def _row(status, title, url, seo=""):
    r = [""] * 48
    r[R.col_to_idx(COL["status"])] = status
    r[R.col_to_idx(COL["title"])] = title
    r[R.col_to_idx(COL["category"])] = "Small Appliances"
    r[R.col_to_idx(COL["costco_cost"])] = "100"
    r[R.col_to_idx(COL["ebay_price"])] = "180"
    r[R.col_to_idx(COL["costco_url"])] = url
    r[R.col_to_idx(COL["seo_title"])] = seo
    return r


@pytest.fixture
def run(monkeypatch):
    calls = {"writes": [], "copy_in": [], "emails": []}
    rows = [_row("ACTIVE", "Live Mixer", "https://www.costco.com/.product.111.html", seo="old eBay title"),
            _row("AUDIT_REVIEW", "Queued Blender", "https://www.costco.com/.product.222.html"),
            _row("PENDING", "Untouched Pending", "https://www.costco.com/.product.333.html")]
    monkeypatch.setattr(R, "get_sheets_service", lambda: "svc")
    monkeypatch.setattr(R, "ensure_grid_columns", lambda *a, **k: None)
    monkeypatch.setattr(R, "read_sheet", lambda svc, rng: rows)
    monkeypatch.setattr(R, "make_browser", lambda: contextlib.nullcontext("page"))
    monkeypatch.setattr(R, "refresh_session", lambda page: None)
    monkeypatch.setattr(R, "scrape_costco", lambda url, page=None: {
        "stock_status": "In Stock", "price": 99.99, "brand": "KitchenAid", "model": "KSM60",
        "in_stock": True, "dimensions": {"length": 14, "width": 11, "height": 17}, "image_urls": []})
    monkeypatch.setattr(R, "get_cart_estimate", lambda url, page=None: {})
    monkeypatch.setattr(R, "get_ebay_comps", lambda *a, **k: {
        "sold_90d": 40, "avg_sold_price": 190.0, "active_count": 12, "median_sold": 185.0,
        "lowest_active": 175.0, "price_basis": "median", "query_strategy": "title"})
    monkeypatch.setattr(R, "get_community_signals", lambda *a, **k: {
        "signal_strength": 3, "summary": "quiet", "source_breakdown": {}, "intent_phrases": []})
    monkeypatch.setattr(R, "_run_claude_research", lambda *a, **k: {
        "dimension_scores": {"margin_potential": 7, "demand_signals": 8, "competition_density": 6,
                             "fulfillment_risk": 7},
        "reasoning": {k: "" for k in ("conservative_analyst", "growth_operator", "brand_builder",
                                      "volume_flipper")},
        "demand_note": ""})
    monkeypatch.setattr(R, "write_row_partial",
                        lambda svc, sheet, row, pairs: calls["writes"].append((row, dict(pairs))))
    monkeypatch.setattr(R, "seed_formula_row", lambda *a, **k: None)
    monkeypatch.setattr(R, "log_sale", lambda *a, **k: None)

    def _copy(batch):
        calls["copy_in"].extend(p["title"] for p in batch)
        return [{"seo_title": f"NEW {p['title']}", "bullets": "• b", "description": "<p>new</p>",
                 "keywords": "k1, k2"} for p in batch]
    monkeypatch.setattr(R, "generate_listing_copy", _copy)
    monkeypatch.setattr(R, "send_alert", lambda *a, **k: calls["emails"].append(a[0]))
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
    monkeypatch.setattr(R, "discover_all", lambda *a, **k: pytest.fail("targeted run must not discover"))

    def go(**kw):
        R.run_researcher(**kw)
        return calls
    return go


def _research_write(calls, row):
    return next(w for r, w in calls["writes"] if r == row and COL["full_notes"] in w)


def test_only_rows_researches_exactly_those_rows_whatever_status(run):
    calls = run(only_rows=[4, 5], notify=False)
    researched = {r for r, w in calls["writes"] if COL["full_notes"] in w}
    assert researched == {4, 5}                      # PENDING row 6 not touched


def test_keep_status_rows_never_write_status_or_re_eval(run):
    calls = run(only_rows=[4, 5], keep_status_rows=[4, 5], notify=False)
    for row in (4, 5):
        w = _research_write(calls, row)
        assert COL["status"] not in w and COL["re_eval_date"] not in w
        assert COL["ebay_listing_url"] not in w
        assert "Costco specs: Brand: KitchenAid | Model: KSM60 | Dimensions: 14 x 11 x 17 in" in w[COL["full_notes"]]
        assert w[COL["costco_cost"]] == 99.99


def test_without_keep_status_the_tier_status_is_written(run):
    calls = run(only_rows=[5], notify=False)
    assert COL["status"] in _research_write(calls, 5)


def test_force_copy_regenerates_existing_copy_and_overrides_win(run):
    calls = run(only_rows=[4, 5], keep_status_rows=[4], force_copy=True, notify=False,
                copy_overrides={4: {"seo_title": "old eBay title", "description": "<p>eBay desc</p>"}})
    assert sorted(calls["copy_in"]) == ["Live Mixer", "Queued Blender"]   # row 4 had copy already
    copy4 = next(w for r, w in calls["writes"] if r == 4 and COL["seo_title"] in w)
    assert copy4[COL["seo_title"]] == "old eBay title" and copy4[COL["description"]] == "<p>eBay desc</p>"
    assert copy4[COL["bullets"]] == "• b" and copy4[COL["keywords"]] == "k1, k2"
    copy5 = next(w for r, w in calls["writes"] if r == 5 and COL["seo_title"] in w)
    assert copy5[COL["seo_title"]] == "NEW Queued Blender"


def test_notify_false_sends_no_email(run):
    assert run(only_rows=[4], notify=False)["emails"] == []


# ── margin = max(dollar, percent) + sale fast-track ─────────────────────────
# The fixture rows hold col G = $100, which wins over the scraped price, so the
# suggested eBay price is what varies the net.

@pytest.fixture
def scored(run, monkeypatch):
    """Composite score forced to Tier 3; returns the research write + what pass 3 saw."""
    seen = {}

    def _pass3(category):
        def fn(dims, cat_config, reasoning=None):
            seen["dims"], seen["fee"] = dict(dims), cat_config.get("fee_rate", 0.1325)
            return {"tier": 3, "weighted_score": 3.1, "recommendation": "PASS",
                    "lens_scores": {"conservative_analyst": 3.0, "volume_flipper": 3.2}}
        return fn
    monkeypatch.setattr(R, "_pass3_for_category", _pass3)

    def go(sugg=130.0, on_sale=True, stock="In Stock"):
        monkeypatch.setattr(R, "_suggest_ebay_price", lambda cost, data, fee: sugg)
        monkeypatch.setattr(R, "get_ebay_comps", lambda *a, **k: {
            "sold_90d": 0, "avg_sold_price": sugg, "active_count": 12, "median_sold": sugg,
            "price_basis": "median", "query_strategy": "title"})
        monkeypatch.setattr(R, "scrape_costco", lambda url, page=None: {
            "stock_status": stock, "price": 100.0, "in_stock": stock == "In Stock",
            "on_sale": on_sale, "original_price": 110.0 if on_sale else None,
            "sale_savings": 10.0 if on_sale else None, "image_urls": []})
        calls = run(only_rows=[5], notify=False)
        seen["net"] = round(sugg * (1 - seen["fee"]) - 100.0, 2)
        seen["real_net"] = R.compute_net(sugg, 100.0, seen["fee"])   # col I formula
        return _research_write(calls, 5), seen
    return go


def test_margin_score_takes_the_better_of_dollar_and_percent(scored):
    _w, seen = scored(on_sale=False)
    dollar = R.score_net_profit(seen["net"])
    pct = R.score_dimension("margin_potential", seen["net"] / 130.0)
    assert pct > dollar                                   # a ~$13 flip at ~10%: percent wins
    assert seen["dims"]["margin_potential"] == max(dollar, pct)


def test_on_sale_in_stock_profitable_item_is_fast_tracked_to_scored(scored):
    w, seen = scored(sugg=150.0)                          # real net ~$20 (simplified ~$30)
    assert seen["real_net"] >= R.SALE_FASTTRACK_MIN_NET
    assert w[COL["status"]] == "SCORED"
    assert (f"Sale fast-track: on sale, net ${seen['real_net']:.2f}/unit"
            in w[COL["full_notes"]])


def test_fast_track_gates_on_real_net_not_simplified(scored):
    # $125 on a $100 item: simplified net ~$8 clears $2, but Costco tax + $0.30 +
    # buyer-tax FVF make the real (col I) net negative
    w, seen = scored(sugg=125.0)
    assert seen["net"] >= R.SALE_FASTTRACK_MIN_NET
    assert seen["real_net"] < R.SALE_FASTTRACK_MIN_NET
    assert w[COL["status"]] == "PAUSED_DEMAND"


def test_fast_track_needs_net_at_floor_on_sale_and_in_stock(scored):
    w, seen = scored(sugg=117.0)                          # simplified net ~$1.50
    assert seen["real_net"] < R.SALE_FASTTRACK_MIN_NET
    assert w[COL["status"]] == "PAUSED_DEMAND"
    assert scored(stock="OUT OF STOCK")[0].get(COL["status"]) != "SCORED"
    assert scored(on_sale=False)[0][COL["status"]] == "PAUSED_DEMAND"
