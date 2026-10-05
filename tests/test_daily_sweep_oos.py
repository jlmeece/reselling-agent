"""run_daily_sweep: restock of a PAUSED_OOS row whose eBay listing the active monitor hid
(qty 0, data/.oos_hidden.json) -> quantity restored + ACTIVE when still profitable at the LIVE
eBay price; otherwise WATCH + Telegram. Never touches eBay (set_quantity / GetItem mocked)."""
from contextlib import contextmanager

import pytest

from agents import scheduler as sch
from tools import sale_schedule as sched

COL = sch.load_col_map()
CONFIG = {"business": {"min_margin_threshold": 0.10, "discount_code": "", "batch_size": 5},
          "categories": {"Pharmacy": {"fee_rate": 0.1325, "ad_rate": 0.02}}}
START_ROW = 4
IID = "123456789012"
EBAY_URL = f"https://www.ebay.com/itm/{IID}"


def _row(**cells):
    row = []
    for key, val in cells.items():
        idx = sch.col_to_idx(COL[key])
        while len(row) <= idx:
            row.append("")
        row[idx] = val
    return row


def _oos_row(ebay_url=EBAY_URL, fee="13.25%", limit=""):
    return _row(status="PAUSED_OOS", title="Seaweed Snack", category="Pharmacy",
                costco_url="https://www.costco.com/x.product.1711796.html",
                costco_cost="19.99", ebay_listing_url=ebay_url, stock_status="OUT OF STOCK",
                fee_rate=fee, ship_cost="0", purchase_limit=limit)


@pytest.fixture
def sweep(monkeypatch):
    calls = {"writes": [], "notify": [], "set_qty": [], "revise_log": []}
    state = {"rows": [], "scrape": None, "info": {}, "set_res": {"ok": True}}
    monkeypatch.setattr(sch, "check_spot_movement", lambda **k: None)
    monkeypatch.setattr(sch, "read_sheet", lambda *a, **k: state["rows"])

    @contextmanager
    def _browser():
        yield object()
    monkeypatch.setattr(sch, "make_browser", _browser)
    monkeypatch.setattr(sch, "scrape_costco", lambda url, page=None: state["scrape"])
    monkeypatch.setattr(sch, "write_row_partial",
                        lambda svc, sheet, row, pairs: calls["writes"].append((row, dict(pairs))))
    monkeypatch.setattr(sch, "run_sale_refresh", lambda *a, **k: {"notes": ""})
    monkeypatch.setattr(sch, "send_routine_alert", lambda **k: None)
    monkeypatch.setattr(sch, "send_ready_to_list_alert", lambda *a, **k: None)
    monkeypatch.setattr(sch, "_notify", lambda text: calls["notify"].append(text) or True)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    monkeypatch.setattr(sch.sys, "platform", "win32")
    monkeypatch.setattr(sch, "fetch_item_price", lambda iid: {
        "ok": True, "item_id": iid, "price": 34.99, "listing_status": "Active",
        "quantity_available": 0, "listing_duration": "GTC", "out_of_stock_control": True,
        "error_kind": None, "error_code": "", "message": "", **state["info"]})

    def _set_qty(iid, qty):
        calls["set_qty"].append((iid, qty))
        return {"item_id": iid, "price": None, "error_kind": None, "error_code": "",
                "message": f"quantity {qty}", **state["set_res"]}
    monkeypatch.setattr(sch, "set_quantity", _set_qty)
    monkeypatch.setattr(sch, "log_revise", lambda svc, res, **k: calls["revise_log"].append((res, k)))

    def run(rows, stock="In Stock", source="inventory_api", price=19.99, info=None, set_res=None):
        for v in calls.values():
            v.clear()
        state["rows"] = rows
        state["scrape"] = {"price": price, "stock_status": stock, "on_sale": False,
                           "image_urls": [], "stock_source": source}
        state["info"] = info or {}
        state["set_res"] = set_res or {"ok": True}
        sch.run_daily_sweep(CONFIG, COL, object(), "Product Tracker", START_ROW, 500)
        return calls
    return run


def _sweep_write(calls):
    (_row_n, w), = calls["writes"]
    return w


def _hide(prev_qty=5, hidden=True):
    sched.record_oos_hidden(IID, title="Seaweed Snack", row=START_ROW, prev_qty=prev_qty, hidden=hidden)


def test_restock_restores_previous_quantity_and_goes_active(sweep):
    _hide(prev_qty=5)
    calls = sweep([_oos_row()])
    assert calls["set_qty"] == [(IID, 5)]
    (_res, kw), = calls["revise_log"]
    assert kw["action"] == "oos_restore" and kw["source"] == "daily"
    w = _sweep_write(calls)
    assert w[COL["status"]] == "ACTIVE"
    assert "quantity restored to 5" in w[COL["tier_summary"]]
    (msg,) = calls["notify"]
    assert "restored 1" in msg and "Seaweed Snack" in msg
    assert sched.get_oos_hidden(IID) is None


@pytest.mark.parametrize("limit, expected", [("4/day", 4), ("", 99)])
def test_unknown_previous_quantity_falls_back_to_purchase_limit_then_99(sweep, limit, expected):
    _hide(prev_qty=None)
    calls = sweep([_oos_row(limit=limit)])
    assert calls["set_qty"] == [(IID, expected)]


def test_restock_at_a_loss_moves_to_watch_without_touching_ebay(sweep):
    _hide()
    calls = sweep([_oos_row()], price=33.00)          # eBay 34.99 vs cost 33 + fees -> loss
    assert calls["set_qty"] == []
    w = _sweep_write(calls)
    assert w[COL["status"]] == "WATCH" and "loses $" in w[COL["tier_summary"]]
    assert "need you" in calls["notify"][0] and "reprice" in calls["notify"][0]
    assert sched.get_oos_hidden(IID) is None


def test_listing_ended_on_ebay_asks_for_relist(sweep):
    _hide(hidden=False)
    calls = sweep([_oos_row()], info={"listing_status": "Completed"})
    assert calls["set_qty"] == []
    w = _sweep_write(calls)
    assert w[COL["status"]] == "WATCH" and "Relist it on eBay" in w[COL["tier_summary"]]
    assert "Relist" in calls["notify"][0]


def test_restock_from_page_text_only_waits(sweep):
    _hide()
    calls = sweep([_oos_row()], source="dom")
    assert calls["set_qty"] == []
    w = _sweep_write(calls)
    assert COL["status"] not in w and "inventory API" in w[COL["tier_summary"]]
    assert calls["notify"] == []
    assert sched.get_oos_hidden(IID) is not None                     # retried tomorrow


def test_getitem_failure_waits(sweep):
    _hide()
    calls = sweep([_oos_row()], info={"ok": False, "error_kind": "network", "price": None})
    assert calls["set_qty"] == []
    assert COL["status"] not in _sweep_write(calls)
    assert sched.get_oos_hidden(IID) is not None


def test_auth_failure_on_restore_waits_and_alerts(sweep):
    _hide()
    calls = sweep([_oos_row()], set_res={"ok": False, "error_kind": "auth", "error_code": "932"})
    assert COL["status"] not in _sweep_write(calls)
    assert "token rejected" in calls["notify"][0]
    assert sched.get_oos_hidden(IID) is not None


def test_other_restore_failure_moves_to_watch(sweep):
    _hide()
    calls = sweep([_oos_row()], set_res={"ok": False, "error_kind": "api", "error_code": "291",
                                         "message": "nope"})
    w = _sweep_write(calls)
    assert w[COL["status"]] == "WATCH" and "restore FAILED (291" in w[COL["tier_summary"]]


def test_still_oos_keeps_the_hidden_record(sweep):
    _hide()
    calls = sweep([_oos_row()], stock="OUT OF STOCK")
    assert COL["status"] not in _sweep_write(calls)
    assert calls["set_qty"] == [] and calls["notify"] == []
    assert sched.get_oos_hidden(IID) is not None


def test_manually_paused_row_keeps_old_restock_to_watch(sweep):
    calls = sweep([_oos_row()])
    w = _sweep_write(calls)
    assert w[COL["status"]] == "WATCH" and calls["set_qty"] == [] and calls["notify"] == []
