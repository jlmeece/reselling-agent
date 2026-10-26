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
