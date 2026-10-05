"""
pytest config: skip standalone debug scripts that match test_*.py but do real
work at import time (launch Chrome / hit live eBay + Claude), which hangs or
errors collection. Run them manually: python tests/<name>.py
"""

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

collect_ignore = [
    "test_scraper.py",
    "test_discovery.py",
    "test_ebay_and_community.py",
    "test_researcher.py",
]


@pytest.fixture(autouse=True)
def _no_live_ebay_taxonomy(monkeypatch, tmp_path):
    """No test may reach live eBay or write data/ebay_taxonomy_cache.json.

    HTTP is blocked at the single seam (so the export falls back to the yaml lists);
    tests that exercise the Taxonomy client re-patch _http_json with a fake."""
    from tools import ebay_taxonomy as et

    def _blocked(req):
        raise urllib.error.URLError("live eBay calls are blocked in tests")

    monkeypatch.setattr(et, "_http_json", _blocked)
    monkeypatch.setattr(et, "CACHE_PATH", tmp_path / "ebay_taxonomy_cache.json")
    monkeypatch.setattr(et, "_sleep", lambda s: None)
    et.reset_state()
    yield
    et.reset_state()


@pytest.fixture(autouse=True)
def _isolated_reprice_state(monkeypatch, tmp_path):
    """No test may touch the real reprice / scheduled-action state or ebay_sync.log:
    data/.sale_end_times.json, data/.scheduled_actions.json, data/.reprice_pending.json."""
    from tools import reprice, sale_schedule

    monkeypatch.setattr(sale_schedule, "SALE_END_PATH", str(tmp_path / "sale_end_times.json"))
    monkeypatch.setattr(sale_schedule, "ACTIONS_PATH", str(tmp_path / "scheduled_actions.json"))
    monkeypatch.setattr(sale_schedule, "OOS_ENDED_PATH", str(tmp_path / "oos_ended.json"))
    monkeypatch.setattr(reprice, "PENDING_PATH", str(tmp_path / "reprice_pending.json"))
    monkeypatch.setattr(reprice, "EBAY_SYNC_LOG", str(tmp_path / "ebay_sync.log"))
