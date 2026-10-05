"""run_daily_sweep: a PAUSED_OOS row whose eBay listing the active monitor auto-ended gets a
"Relist it on eBay" note + Telegram line on restock (col Q points at a dead listing)."""
from contextlib import contextmanager

import pytest

from agents import scheduler as sch
from tools import sale_schedule as sched

COL = sch.load_col_map()
CONFIG = {"business": {"min_margin_threshold": 0.10, "discount_code": "", "batch_size": 5},
          "categories": {}}
START_ROW = 4
EBAY_URL = "https://www.ebay.com/itm/123456789012"


def _row(**cells):
    row = []
    for key, val in cells.items():
        idx = sch.col_to_idx(COL[key])
        while len(row) <= idx:
            row.append("")
        row[idx] = val
    return row


def _oos_row(ebay_url=EBAY_URL):
    return _row(status="PAUSED_OOS", title="Seaweed Snack", category="Pharmacy",
                costco_url="https://www.costco.com/x.product.1711796.html",
                costco_cost="19.99", ebay_listing_url=ebay_url, stock_status="OUT OF STOCK")


@pytest.fixture
def sweep(monkeypatch):
    calls = {"writes": [], "notify": []}
    state = {"rows": [], "stock": "In Stock"}
    monkeypatch.setattr(sch, "check_spot_movement", lambda **k: None)
    monkeypatch.setattr(sch, "read_sheet", lambda *a, **k: state["rows"])

    @contextmanager
    def _browser():
        yield object()
    monkeypatch.setattr(sch, "make_browser", _browser)
    monkeypatch.setattr(sch, "scrape_costco", lambda url, page=None: {
        "price": 19.99, "stock_status": state["stock"], "on_sale": False, "image_urls": []})
    monkeypatch.setattr(sch, "write_row_partial",
                        lambda svc, sheet, row, pairs: calls["writes"].append((row, dict(pairs))))
    monkeypatch.setattr(sch, "run_sale_refresh", lambda *a, **k: {"notes": ""})
    monkeypatch.setattr(sch, "send_routine_alert", lambda **k: None)
    monkeypatch.setattr(sch, "send_ready_to_list_alert", lambda *a, **k: None)
    monkeypatch.setattr(sch, "_notify", lambda text: calls["notify"].append(text) or True)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    monkeypatch.setattr(sch.sys, "platform", "win32")

    def run(rows, stock="In Stock"):
        state["rows"], state["stock"] = rows, stock
        sch.run_daily_sweep(CONFIG, COL, object(), "Product Tracker", START_ROW, 500)
        return calls
    return run


def _sweep_write(calls):
    (row, w), = [c for c in calls["writes"] if COL["status"] in c[1] or COL["tier_summary"] in c[1]]
    return w


def test_restock_of_auto_ended_listing_asks_for_relist(sweep):
    sched.record_oos_ended("123456789012", title="Seaweed Snack", row=START_ROW)
    calls = sweep([_oos_row()])
    w = _sweep_write(calls)
    assert w[COL["status"]] == "WATCH"
    assert "Relist it on eBay" in w[COL["tier_summary"]] and "123456789012" in w[COL["tier_summary"]]
    (msg,) = calls["notify"]
    assert "Seaweed Snack" in msg and "123456789012" in msg
    assert sched.pop_oos_ended("123456789012") is None              # consumed once


def test_restock_of_manually_paused_row_has_no_relist_note(sweep):
    calls = sweep([_oos_row()])
    w = _sweep_write(calls)
    assert w[COL["status"]] == "WATCH"
    assert "Relist" not in w[COL["tier_summary"]]
    assert calls["notify"] == []


def test_still_oos_keeps_the_ended_record(sweep):
    sched.record_oos_ended("123456789012", title="Seaweed Snack", row=START_ROW)
    calls = sweep([_oos_row()], stock="OUT OF STOCK")
    w = _sweep_write(calls)
    assert COL["status"] not in w
    assert calls["notify"] == []
    assert sched.pop_oos_ended("123456789012") is not None
