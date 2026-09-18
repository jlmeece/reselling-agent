"""Tests for tools/mpt_engine.py — Phase 1 Sharpe ranking."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mpt_engine import rank_products, sharpe_label, HURDLE_RATE, _parse_sold_range, rank_rows_by_sharpe


def _p(**kwargs):
    """Build a minimal product dict with sensible defaults."""
    base = {
        "title": "Test Product",
        "category": "Jewelry",
        "costco_cost": 100.0,
        "ebay_price": 130.0,
        "sold_90d": 10,
        "avg_sold_price": 125.0,
        "sold_range": None,
        "active_count": 5,
        "fee_rate": 0.15,
        "ship_cost": 0.0,
        "ad_cost": 0.0,
    }
    base.update(kwargs)
    return base


def test_beats_hurdle():
    """40% markup, 30 sold/90d, no fees → annualized return well above 25% hurdle."""
    r = rank_products([_p(
        costco_cost=100.0,
        ebay_price=140.0,
        sold_90d=30,
        fee_rate=0.0,
        ship_cost=0.0,
        ad_cost=0.0,
    )])[0]

    # mu = (40/100) * (365/3) ≈ 486%  — fast velocity amplifies margin
    assert r["beats_hurdle"] is True, f"Expected beats_hurdle=True, got {r['beats_hurdle']}"
    assert r["sharpe"] > 0, f"Expected sharpe > 0, got {r['sharpe']}"
    assert r["mu"] > HURDLE_RATE, f"Expected mu > {HURDLE_RATE}, got {r['mu']}"
    assert r["days_to_sell_est"] == 3


def test_below_hurdle():
    """2% markup, slow velocity (2 sold/90d), no fees → annualized return < 25% hurdle."""
    r = rank_products([_p(
        costco_cost=100.0,
        ebay_price=102.0,   # 2% gross — annualized ≈ 16% at 45-day velocity, well below 25%
        sold_90d=2,
        fee_rate=0.0,
        ship_cost=0.0,
        ad_cost=0.0,
    )])[0]

    # mu = (2/100) * (365/45) ≈ 16.2% < 25% hurdle
    assert r["beats_hurdle"] is False, f"Expected beats_hurdle=False, got {r['beats_hurdle']}"
    assert r["sharpe"] < 0, f"Expected sharpe < 0, got {r['sharpe']}"
    assert r["days_to_sell_est"] == 45


def test_rank_ordering():
    """3 products supplied in arbitrary order → output sorted by sharpe descending, ranks assigned 1-2-3."""
    low = _p(title="Low",  costco_cost=100, ebay_price=101, sold_90d=1,  fee_rate=0.0, ship_cost=0, ad_cost=0)
    mid = _p(title="Mid",  costco_cost=100, ebay_price=125, sold_90d=10, fee_rate=0.0, ship_cost=0, ad_cost=0)
    high = _p(title="High", costco_cost=100, ebay_price=145, sold_90d=30, fee_rate=0.0, ship_cost=0, ad_cost=0)

    results = rank_products([low, mid, high])

    sharpes = [r["sharpe"] for r in results]
    assert sharpes == sorted(sharpes, reverse=True), f"Not sorted descending: {sharpes}"
    assert [r["mpt_rank"] for r in results] == [1, 2, 3]
    assert results[0]["title"] == "High", f"Expected 'High' first, got '{results[0]['title']}'"
    assert results[2]["title"] == "Low", f"Expected 'Low' last, got '{results[2]['title']}'"


def test_parse_sold_range_en_dash():
    """Parse '$2,600–$2,760' (en dash) into (2600.0, 2760.0)."""
    result = _parse_sold_range("$2,600–$2,760")
    assert result == (2600.0, 2760.0), f"Got {result}"


def test_parse_sold_range_hyphen():
    """Parse '$850-$1200' (ASCII hyphen) into (850.0, 1200.0)."""
    result = _parse_sold_range("$850-$1200")
    assert result == (850.0, 1200.0), f"Got {result}"


def test_parse_sold_range_none():
    """None or empty string → None."""
    assert _parse_sold_range(None) is None
    assert _parse_sold_range("") is None


def test_sharpe_labels():
    assert sharpe_label(2.5) == "🔥 Strong"
    assert sharpe_label(2.0) == "🔥 Strong"
    assert sharpe_label(1.5) == "✅ Good"
    assert sharpe_label(1.0) == "✅ Good"
    assert sharpe_label(0.5) == "⚠️ Marginal"
    assert sharpe_label(0.0) == "⚠️ Marginal"
    assert sharpe_label(-0.1) == "❌ Below hurdle"


def test_precious_metals_tight_sigma():
    """Precious metals with 15+ comps should have sigma close to observed volatility, not jewelry prior."""
    pm = _p(
        category="Precious Metals",
        costco_cost=2400.0,
        ebay_price=2700.0,
        sold_90d=20,
        avg_sold_price=2680.0,
        sold_range="$2,640–$2,720",  # tight: (80/2680) ≈ 0.030 CV
        fee_rate=0.04,
        ship_cost=0.0,
        ad_cost=0.0,
    )
    r = rank_products([pm])[0]
    # shrink_weight = 15/15 = 1.0 → sigma = price_volatility ≈ 0.030
    # Prior for PM is 0.08, so sigma should be between 0.03 and 0.08
    assert r["sigma"] < 0.15, f"Precious metals sigma too high: {r['sigma']}"
    assert r["beats_hurdle"] is True


def test_no_sales_pessimistic():
    """sold_90d=0 → days_to_sell=90 (full 90-day pessimistic assumption)."""
    r = rank_products([_p(
        costco_cost=100.0,
        ebay_price=150.0,
        sold_90d=0,
        fee_rate=0.0,
        ship_cost=0.0,
        ad_cost=0.0,
    )])[0]
    assert r["days_to_sell_est"] == 90
    # mu = (50/100) * (365/90) ≈ 202% — still beats hurdle even pessimistically
    assert r["beats_hurdle"] is True


def test_rank_rows_by_sharpe_normal():
    """3 rows with distinct sharpe text, arbitrary input order → sorted descending, ranks 1-2-3."""
    rows = [
        (10, "0.5000 ⚠️ Marginal"),
        (4,  "2.1000 🔥 Strong"),
        (7,  "1.2000 ✅ Good"),
    ]
    result = rank_rows_by_sharpe(rows)
    assert result == [(4, 1), (7, 2), (10, 3)], f"Got {result}"


def test_rank_rows_by_sharpe_skips_blank():
    """Blank/None/empty-string cells are excluded; remaining rows still get contiguous ranks."""
    rows = [
        (4, "1.0000 ✅ Good"),
        (5, ""),
        (6, None),
        (7, "2.0000 🔥 Strong"),
        (8, "   "),
    ]
    result = rank_rows_by_sharpe(rows)
    assert result == [(7, 1), (4, 2)], f"Got {result}"


def test_rank_rows_by_sharpe_parses_label_suffix():
    """Leading float (including negative sign) parses correctly; emoji/label suffix is ignored."""
    rows = [
        (4, "1.2345 🔥 Strong"),
        (5, "-0.5 ❌ Below hurdle"),
    ]
    result = rank_rows_by_sharpe(rows)
    assert result == [(4, 1), (5, 2)], f"Got {result}"


if __name__ == "__main__":
    tests = [
        test_beats_hurdle,
        test_below_hurdle,
        test_rank_ordering,
        test_parse_sold_range_en_dash,
        test_parse_sold_range_hyphen,
        test_parse_sold_range_none,
        test_sharpe_labels,
        test_precious_metals_tight_sigma,
        test_no_sales_pessimistic,
        test_rank_rows_by_sharpe_normal,
        test_rank_rows_by_sharpe_skips_blank,
        test_rank_rows_by_sharpe_parses_label_suffix,
    ]
    print(f"Running {len(tests)} tests...\n")
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed.")
