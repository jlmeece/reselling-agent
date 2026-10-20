"""tools/ebay_research.py — lowest_active capture + lowest-first active search URL.
_scrape_ebay_page is stubbed; nothing touches eBay or Playwright."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import ebay_research


def _comps(monkeypatch, active_prices, sold_prices=None):
    sold_prices = [50.0] * 12 if sold_prices is None else sold_prices

    def fake_scrape(page, url, label):
        return (len(sold_prices), list(sold_prices)) if label == "sold" else (len(active_prices), list(active_prices))
    monkeypatch.setattr(ebay_research, "_scrape_ebay_page", fake_scrape)
    return ebay_research.get_ebay_comps("Widget Deluxe 3000 Stainless", page=MagicMock())


def test_active_url_sorts_lowest_first_sold_url_unchanged():
    sold, active = ebay_research._build_search_urls("widget", 0)
    assert "&_sop=15" in active
    assert "_sop" not in sold


def test_lowest_active_is_raw_min_not_outlier_trimmed(monkeypatch):
    prices = [19.99] + [40.0 + i for i in range(19)]   # 19.99 sits in the bottom 10%
    result = _comps(monkeypatch, prices)
    assert result["lowest_active"] == 19.99
    assert result["median_active"] is not None


def test_lowest_active_none_when_no_active_prices(monkeypatch):
    result = _comps(monkeypatch, [])
    assert result["lowest_active"] is None
