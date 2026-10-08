"""run_active_monitor, sale-aware (mocked sheet / browser / scraper / alerts).

Energy Shot (4000099948): regular $39.99, on sale $31.99. Sale start must write G/AW/X and
NOT flag col P or alert; the reverse ($31.99 -> $39.99) must flag P and send the urgent alert."""
from contextlib import contextmanager
from datetime import datetime

import pytest

from agents import scheduler as sch

COL = sch.load_col_map()
CONFIG = {"business": {"price_change_threshold": 0.50, "min_margin_threshold": 0.10,
                       "min_demand_score": 5, "sale_warn_hours": 48, "sale_urgent_hours": 24},
          "categories": {}}
START_ROW = 4
NOW = datetime(2026, 9, 24, 12, 0)      # frozen clock: expiry tiers depend on hours-to-23:59 of a DATE


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def _row(**cells):
    row = []
    for key, val in cells.items():
        idx = sch.col_to_idx(COL[key])
        while len(row) <= idx:
            row.append("")
        row[idx] = val
    return row


def _energy_row(cost, badge="", regular="", flag="", ebay_price="59.99", ebay_url="", status="ACTIVE"):
    return _row(status=status, title="Kirkland Signature Energy Shot", category="Pharmacy",
                ebay_listing_url=ebay_url,
                costco_url="https://www.costco.com/x.product.1711796.html",
                costco_cost=str(cost), ebay_price=ebay_price, fee_rate="13.25%", ship_cost="0",
                demand_score="8", sale_info=badge, regular_price=regular, price_change=flag,
                stock_status="In Stock")


def _approved_row():
    return _row(status="APPROVED", title="Kirkland Signature Energy Shot", category="Pharmacy",
                costco_url="https://www.costco.com/x.product.1711796.html",
                image_urls="")


def _scrape(price, on_sale, original=None, savings=None, expires=None,
            coupon_type=None, coupon_label=None, stock_status="In Stock", stock_source=None):
    return {"price": price, "stock_status": stock_status, "image_urls": ["http://img/1.jpg"],
            "on_sale": on_sale, "original_price": original, "sale_savings": savings,
            "sale_expires": expires, "coupon_type": coupon_type, "coupon_label": coupon_label,
            "stock_source": stock_source, "error": None}


@pytest.fixture
def harness(monkeypatch):
    calls = {"writes": [], "urgent": [], "expiry": [], "sales": [], "recorded": [],
             "prompts": [], "pending": [], "ended": [], "revise_log": [], "notify": []}
    # set_quantity_zero result per call (list consumed in order; default ok) + the GetItem view
    # of the listing — never reaches eBay.
    state_end = {"results": [], "info": None}

    def _hide(item_id):
        calls["ended"].append(item_id)
        res = state_end["results"].pop(0) if state_end["results"] else {"ok": True}
        return {"item_id": item_id, "price": None, "error_kind": None, "error_code": "",
                "message": "quantity 0", **res}
    monkeypatch.setattr(sch, "set_quantity_zero", _hide)
    monkeypatch.setattr(sch, "fetch_item_price", lambda iid: {
        "ok": True, "item_id": iid, "price": 59.99, "listing_status": "Active",
        "quantity_available": 7, "listing_duration": "GTC", "out_of_stock_control": True,
        "error_kind": None, "error_code": "", "message": "", **(state_end["info"] or {})})
    monkeypatch.setattr(sch, "log_revise", lambda svc, res, **k: calls["revise_log"].append((res, k)))
    monkeypatch.setattr(sch, "_notify", lambda text: calls["notify"].append(text) or True)
    state = {"rows": [], "scrape": None}

    monkeypatch.setattr(sch, "read_sheet", lambda *a, **k: state["rows"])

    @contextmanager
    def _browser():
        yield object()
    monkeypatch.setattr(sch, "make_browser", _browser)
    monkeypatch.setattr(sch, "scrape_costco", lambda url, page=None: state["scrape"])
    monkeypatch.setattr(sch, "write_row_partial",
                        lambda svc, sheet, row, pairs: calls["writes"].append((row, dict(pairs))))
    monkeypatch.setattr(sch, "log_sale", lambda *a, **k: calls["sales"].append((a, k)) or True)
    monkeypatch.setattr(sch, "send_urgent_alert",
                        lambda subject, items, run_time=None, **k: calls["urgent"].append((subject, items)))
    monkeypatch.setattr(sch, "send_sale_expiry_alert",
                        lambda products, hours_remaining: calls["expiry"].append((products, hours_remaining)))
    monkeypatch.setattr(sch, "load_alert_state", lambda: {})
    monkeypatch.setattr(sch, "record_alerts", lambda st, keys, **k: calls["recorded"].extend(keys))
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    monkeypatch.setattr(sch, "send_reprice_prompt", lambda item: calls["prompts"].append(item) or True)
    monkeypatch.setattr(sch, "save_pending", lambda item: calls["pending"].append(item))
    monkeypatch.setattr(sch, "datetime", _FixedDT)

    def run(rows, scrape, only_rows=None, config=CONFIG, end_results=(), item_info=None):
        for key in calls:
            calls[key].clear()
        state["rows"], state["scrape"] = rows, scrape
        state_end["results"], state_end["info"] = list(end_results), item_info
        sch.run_active_monitor(config, COL, object(), "Product Tracker", START_ROW, 500,
                               only_rows=only_rows)
        return calls
    return run


def _written(calls):
    assert len(calls["writes"]) == 1
    return calls["writes"][0]


def test_sale_start_writes_g_aw_x_and_does_not_reprice(harness):
    calls = harness([_energy_row(39.99)],
                    _scrape(31.99, True, original=39.99, savings=8.0, expires="10/18/26"))
    row, w = _written(calls)
    assert row == START_ROW
    assert w[COL["costco_cost"]] == 31.99
    assert w[COL["regular_price"]] == 39.99
    assert w[COL["sale_info"]] == "🔥 -$8 ends 10/18/26"
    assert COL["price_change"] not in w                      # no P write at all
    assert "on sale — margin +$8.00" in w[COL["tier_summary"]]
    assert calls["urgent"] == []                             # no reprice suggestion / alert
    assert len(calls["sales"]) == 1                          # Sale History logged


def test_sale_start_passes_coupon_type_to_sale_history(harness):
    calls = harness([_energy_row(39.99)],
                    _scrape(31.99, True, original=39.99, savings=8.0, expires="10/18/26",
                            coupon_type="MFR", coupon_label="Manufacturer Coupon"))
    (_args, kwargs), = calls["sales"]
    assert kwargs == {"coupon_type": "MFR", "coupon_label": "Manufacturer Coupon"}


def test_sale_end_rise_flags_p_and_sends_urgent_reprice_up(harness):
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/14/26", regular="39.99", ebay_price="41.48")],
                    _scrape(39.99, False))
    row, w = _written(calls)
    assert w[COL["costco_cost"]] == 39.99
    assert w[COL["price_change"]] == "YES — update listing"
    assert w[COL["sale_info"]] == "" and w[COL["regular_price"]] == ""
    assert len(calls["urgent"]) == 1
    _subject, items = calls["urgent"][0]
    assert len(items) == 1
    assert "sale ended" in items[0]["reason"] and "$31.99→$39.99" in items[0]["reason"]
    target = sch.suggest_reprice(39.99, 0.1325, 0.0)
    assert f"${target:.2f}" in items[0]["reason"]


def test_sale_end_is_quiet_when_listing_price_already_covers_margin(harness):
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/18/26", ebay_price="79.99")],
                    _scrape(39.99, False))
    _row_no, w = _written(calls)
    assert COL["price_change"] not in w
    assert calls["urgent"] == []
    assert "already covers margin" in w[COL["tier_summary"]]


def test_trivial_drift_updates_g_silently(harness):
    calls = harness([_energy_row(39.99)], _scrape(40.20, False))
    _r, w = _written(calls)
    assert w[COL["costco_cost"]] == 40.20
    assert COL["price_change"] not in w and COL["sale_info"] not in w
    assert calls["urgent"] == []


def test_stale_badge_without_cost_move_is_cleared_silently(harness):
    calls = harness([_energy_row(39.99, badge="🔥 -$100", regular="139.99")], _scrape(39.99, False))
    _r, w = _written(calls)
    assert w[COL["sale_info"]] == "" and w[COL["regular_price"]] == ""
    assert calls["urgent"] == []


def test_scrape_without_price_never_looks_like_a_sale_end(harness):
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/18/26", regular="39.99")],
                    _scrape(None, False))
    _r, w = _written(calls)
    assert COL["costco_cost"] not in w and COL["sale_info"] not in w and COL["regular_price"] not in w
    assert calls["urgent"] == []


def test_existing_p_flag_survives_a_quiet_run_until_repriced(harness):
    calls = harness([_energy_row(39.99, flag="YES — update listing", ebay_price="41.48")],
                    _scrape(39.99, False))
    _r, w = _written(calls)
    # still losing at eBay $41.48 -> flag kept (never blanked); the losing-money path re-asserts it
    assert w.get(COL["price_change"], "YES — update listing") == "YES — update listing"
    calls = harness([_energy_row(39.99, flag="YES — update listing", ebay_price="79.99")],
                    _scrape(39.99, False))
    _r, w2 = _written(calls)
    assert w2[COL["price_change"]] == ""                      # listing fixed -> flag cleared


def test_expiry_countdown_fires_from_freshly_written_sale_data(harness):
    exp = "9/25/26"      # 23:59 tomorrow = ~36h after the frozen 12:00 -> 48h "warn" tier
    calls = harness([_energy_row(39.99)],
                    _scrape(31.99, True, original=39.99, savings=8.0, expires=exp))
    # the row had no badge in the sheet before this run; the badge written THIS run is counted
    assert len(calls["expiry"]) == 1
    products, _hours = calls["expiry"][0]
    assert products[0]["sale_expires"] == exp and products[0]["regular_costco_cost"] == 39.99
    assert calls["recorded"][0].endswith("|warn") or calls["recorded"][0].endswith("|urgent")


def test_expiry_urgent_tier_inside_24h(harness):
    exp = "9/24/26"      # 23:59 today = ~12h after the frozen 12:00 -> "urgent" tier
    calls = harness([_energy_row(31.99, badge=f"🔥 -$8 ends {exp}", regular="39.99")],
                    _scrape(31.99, True, original=39.99, savings=8.0, expires=exp))
    assert calls["recorded"][0].endswith("|urgent")


def test_only_rows_limits_scrape_and_alerts(harness):
    rows = [_energy_row(39.99), _energy_row(39.99)]
    calls = harness(rows, _scrape(31.99, True, original=39.99, savings=8.0, expires="10/18/26"),
                    only_rows={START_ROW + 1})
    assert [r for r, _w in calls["writes"]] == [START_ROW + 1]


def test_negative_margin_active_row_is_not_paused_and_alerts_losing_money(harness):
    # cost 31.99 vs eBay 33.99 at a 13.25% fee -> net -2.50, cost UNCHANGED (no sale ended)
    calls = harness([_energy_row(31.99, ebay_price="33.99")], _scrape(31.99, False))
    _r, w = _written(calls)
    assert COL["status"] not in w                                   # stays ACTIVE — no PAUSED_MARGIN write
    assert "losing money" in w[COL["tier_summary"]]                 # negative margin -> col T note
    assert w[COL["price_change"]] == "YES — update listing"
    assert len(calls["urgent"]) == 1                                # 1976458: losing listing = urgent
    item = calls["urgent"][0][1][0]
    assert item["reason"].startswith("losing money — net $-2.50 at $33.99")
    assert "sale ended" not in item["reason"] and "→" not in item["reason"]   # no fake cost move
    assert item["target"] and "Reprice eBay to" in item["reprice_note"]


def test_oos_active_row_still_pauses_and_alerts(harness):
    scrape = _scrape(31.99, False)
    scrape["stock_status"] = "OUT OF STOCK"
    calls = harness([_energy_row(31.99)], scrape)
    _r, w = _written(calls)
    assert w[COL["status"]] == "PAUSED_OOS"
    assert len(calls["urgent"]) == 1 and "OUT OF STOCK" in calls["urgent"][0][1][0]["reason"]


def test_approved_row_backfills_image_urls(harness):
    """APPROVED rows self-heal a blank/stale col AT from fresh scrape image URLs."""
    calls = harness([_approved_row()], _scrape(31.99, False))
    _r, w = _written(calls)
    assert w[COL["stock_status"]] == "In Stock"
    assert COL["last_checked"] in w
    assert w[COL["image_urls"]] == "http://img/1.jpg"
    assert w[COL["costco_cost"]] == 31.99


def test_approved_row_skips_image_write_when_scrape_has_none(harness):
    scrape = _scrape(31.99, False)
    scrape["image_urls"] = []
    calls = harness([_approved_row()], scrape)
    _r, w = _written(calls)
    assert COL["image_urls"] not in w


# ── one-tap reprice prompt ───────────────────────────────────────────────────

EBAY_URL = "https://www.ebay.com/itm/123456789012"


def test_sale_end_on_live_listing_sends_one_reprice_prompt(harness):
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/14/26", regular="39.99",
                                 ebay_price="41.48", ebay_url=EBAY_URL)],
                    _scrape(39.99, False))
    (prompt,) = calls["prompts"]
    assert prompt["item_id"] == "123456789012" and prompt["row"] == START_ROW
    assert (prompt["old_cost"], prompt["new_cost"], prompt["ebay_price"]) == (31.99, 39.99, 41.48)
    assert prompt["target"] == sch.restore_margin_price(31.99, 39.99, 41.48, 0.1325, 0.0) == 50.99
    assert calls["pending"] == [prompt]                       # saved for a re-send
    _, w = _written(calls)
    assert w[COL["price_change"]] == "YES — update listing"   # flag still set until the tap
    assert COL["ebay_price"] not in w                         # monitor NEVER changes col H
    (_, items), = calls["urgent"]
    assert "$50.99" in items[0]["reason"]                     # alert quotes the same price


def test_no_prompt_without_ebay_item_id(harness):
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/14/26", ebay_price="41.48")],
                    _scrape(39.99, False))
    assert calls["prompts"] == [] and len(calls["urgent"]) == 1


def test_no_prompt_on_sale_start(harness):
    calls = harness([_energy_row(39.99, ebay_price="41.48", ebay_url=EBAY_URL)],
                    _scrape(31.99, True, original=39.99, savings=8.0, expires="10/18/26"))
    assert calls["prompts"] == [] and calls["pending"] == []


def test_no_prompt_for_non_active_row(harness):
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/14/26", ebay_price="41.48",
                                 ebay_url=EBAY_URL, status="READY")],
                    _scrape(39.99, False))
    assert calls["prompts"] == []


def test_no_prompt_when_listing_already_covers_margin(harness):
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/18/26", ebay_price="79.99",
                                 ebay_url=EBAY_URL)],
                    _scrape(39.99, False))
    assert calls["prompts"] == []


# ── scheduled reprice interplay ──────────────────────────────────────────────

from tools import sale_schedule as sched  # noqa: E402


def test_monitor_records_and_clears_exact_sale_end(harness):
    scrape = _scrape(31.99, True, original=39.99, savings=8.0, expires="10/18/26")
    scrape["sale_end_ts"] = "2026-10-19T06:59:00+00:00"
    harness([_energy_row(39.99, ebay_url=EBAY_URL)], scrape)       # URL x.product.1711796.html
    assert sched.get_sale_end("1711796")["end_ts"] == "2026-10-19T06:59:00+00:00"
    # the /p/-/slug/<id> URL shape is keyed by its trailing product id
    harness([_row(status="ACTIVE", title="Energy Shot", category="Pharmacy",
                  costco_url="https://www.costco.com/p/-/energy-shot/4000099948",
                  costco_cost="39.99", ebay_price="41.48", fee_rate="13.25%", ship_cost="0",
                  stock_status="In Stock")], scrape)
    assert sched.get_sale_end("4000099948")["end_ts"] == "2026-10-19T06:59:00+00:00"
    harness([_row(status="ACTIVE", title="Energy Shot", category="Pharmacy",
                  costco_url="https://www.costco.com/p/-/energy-shot/4000099948",
                  costco_cost="31.99", ebay_price="41.48", fee_rate="13.25%", ship_cost="0",
                  stock_status="In Stock")], _scrape(39.99, False))
    assert sched.get_sale_end("4000099948") is None                 # sale over -> cleared


def test_pending_scheduled_action_is_fast_forwarded_not_reprompted(harness):
    sched.schedule_action("123456789012", "reprice", {"target": 50.99,
                          "sale_end_ts": "2099-01-01T00:00:00+00:00"})
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/14/26", regular="39.99",
                                 ebay_price="41.48", ebay_url=EBAY_URL)], _scrape(39.99, False))
    assert calls["prompts"] == []                                   # scheduled path owns it
    assert sched.due_actions()                                      # early revert -> due now


def test_recent_scheduled_reprice_suppresses_reactive_prompt(harness):
    sched.schedule_action("123456789012", "reprice", {"target": 50.99,
                          "sale_end_ts": "2026-10-04T00:00:00+00:00"})
    sched.mark_applied("123456789012", {"ok": True})
    calls = harness([_energy_row(31.99, badge="🔥 -$8 ends 10/14/26", regular="39.99",
                                 ebay_price="41.48", ebay_url=EBAY_URL)], _scrape(39.99, False))
    assert calls["prompts"] == []


# ── auto-hide on Costco OOS (business.ebay_sync.auto_end_oos) ────────────────

EBAY_URL_2 = "https://www.ebay.com/itm/210987654321"


def _oos(source="inventory_api"):
    return _scrape(31.99, False, stock_status="OUT OF STOCK", stock_source=source)


def test_oos_live_listing_is_hidden_qty_zero_and_not_alerted(harness):
    sched.schedule_action("123456789012", "reprice", {"target": 50.99,
                          "sale_end_ts": "2099-01-01T00:00:00+00:00"})
    from tools import reprice
    reprice.save_pending({"item_id": "123456789012", "target": 50.99})
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)], _oos())
    assert calls["ended"] == ["123456789012"]
    (res, kw), = calls["revise_log"]
    assert kw["source"] == "active_oos" and kw["action"] == "oos_hide" and kw["row"] == START_ROW
    _, w = _written(calls)
    assert w[COL["status"]] == "PAUSED_OOS"                       # sweep keeps watching for restock
    assert w[COL["tier_summary"]].startswith("OUT OF STOCK — eBay listing hidden (quantity 0)")
    assert "pause eBay listing" not in w[COL["tier_summary"]]
    assert calls["urgent"] == []                                  # nothing for Jay to do
    (summary,) = calls["notify"]
    assert "hid 1 eBay listing" in summary and "Energy Shot" in summary
    # follow-ups for a hidden listing are dropped; the sweep knows what to restore
    assert sched.get_action("123456789012") is None
    assert reprice.get_pending("123456789012") is None
    rec = sched.get_oos_hidden("123456789012")
    assert rec["row"] == START_ROW and rec["prev_qty"] == 7 and rec["hidden"] is True


@pytest.mark.parametrize("info, why", [
    ({"listing_duration": "Days_30"}, "Days_30, not GTC"),
    ({"out_of_stock_control": False}, "Out of Stock control is off"),
])
def test_qty_zero_on_non_gtc_or_oos_control_off_says_ebay_ended_it(harness, info, why):
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)], _oos(), item_info=info)
    _, w = _written(calls)
    assert why in w[COL["tier_summary"]] and "ENDED" in w[COL["tier_summary"]]
    assert calls["urgent"] == []
    assert "eBay ENDED it" in calls["notify"][0]
    assert sched.get_oos_hidden("123456789012")["hidden"] is False


def test_listing_already_ended_on_ebay_sends_nothing(harness):
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)], _oos(),
                    item_info={"listing_status": "Completed"})
    assert calls["ended"] == [] and calls["revise_log"] == []
    _, w = _written(calls)
    assert w[COL["status"]] == "PAUSED_OOS" and "already Completed" in w[COL["tier_summary"]]
    assert calls["urgent"] == []


def test_getitem_down_still_hides(harness):
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)], _oos(),
                    item_info={"ok": False, "price": None, "listing_status": "",
                               "quantity_available": None, "listing_duration": "",
                               "out_of_stock_control": None, "error_kind": "network"})
    assert calls["ended"] == ["123456789012"] and calls["urgent"] == []
    assert sched.get_oos_hidden("123456789012")["prev_qty"] is None


def test_auth_error_on_getitem_stops_auto_hide(harness):
    rows = [_energy_row(31.99, ebay_url=EBAY_URL), _energy_row(31.99, ebay_url=EBAY_URL_2)]
    calls = harness(rows, _oos(), item_info={"ok": False, "error_kind": "auth",
                                             "error_code": "932", "message": "token expired"})
    assert calls["ended"] == []
    (_, items), = calls["urgent"]
    assert len(items) == 2
    assert "auto-hide FAILED (932" in items[0]["reason"]
    assert "pause eBay listing" in items[1]["reason"]
    assert len(calls["notify"]) == 1 and "token rejected" in calls["notify"][0]


def test_oos_flag_off_alerts_only(harness):
    cfg = {**CONFIG, "business": {**CONFIG["business"], "ebay_sync": {"auto_end_oos": False}}}
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)], _oos(), config=cfg)
    assert calls["ended"] == [] and calls["notify"] == []
    (_, items), = calls["urgent"]
    assert "pause eBay listing" in items[0]["reason"]


def test_oos_without_item_id_alerts_only(harness):
    calls = harness([_energy_row(31.99)], _oos())
    assert calls["ended"] == []
    assert len(calls["urgent"]) == 1 and "pause eBay listing" in calls["urgent"][0][1][0]["reason"]


def test_oos_from_page_text_only_is_not_hidden(harness):
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)], _oos(source="dom"))
    assert calls["ended"] == []
    _, w = _written(calls)
    assert w[COL["status"]] == "PAUSED_OOS"
    (_, items), = calls["urgent"]
    assert "NOT auto-hidden" in items[0]["reason"]


def test_auth_error_on_hide_stops_auto_hide_for_the_rest_of_the_run(harness):
    rows = [_energy_row(31.99, ebay_url=EBAY_URL), _energy_row(31.99, ebay_url=EBAY_URL_2)]
    calls = harness(rows, _oos(), end_results=[{"ok": False, "error_kind": "auth",
                                                "error_code": "932", "message": "token expired"}])
    assert calls["ended"] == ["123456789012"]                     # second row never tried
    (_, items), = calls["urgent"]
    assert len(items) == 2
    assert "auto-hide FAILED (932" in items[0]["reason"]
    assert "pause eBay listing" in items[1]["reason"]
    assert len(calls["notify"]) == 1 and "token rejected" in calls["notify"][0]
    assert sched.get_oos_hidden("123456789012") is None


def test_non_auth_failure_falls_back_to_urgent_alert(harness):
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)], _oos(),
                    end_results=[{"ok": False, "error_kind": "api", "error_code": "21916799",
                                  "message": "SKU required for variations"}])
    (_, items), = calls["urgent"]
    assert "auto-hide FAILED (21916799: SKU required" in items[0]["reason"]
    assert calls["notify"] == []                                  # no "hid" summary


def test_unexpected_exception_falls_back_to_urgent_alert(harness, monkeypatch):
    def boom(item_id):
        raise RuntimeError("kaput")
    monkeypatch.setattr(sch, "set_quantity_zero", boom)
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)], _oos())
    (_, items), = calls["urgent"]
    assert "auto-hide FAILED" in items[0]["reason"]


@pytest.mark.parametrize("stock", ["Limited", "CHECK FAILED", "In Stock"])
def test_non_oos_never_touches_listing(harness, stock):
    calls = harness([_energy_row(31.99, ebay_url=EBAY_URL)],
                    _scrape(31.99, False, stock_status=stock, stock_source="inventory_api"))
    assert calls["ended"] == []
