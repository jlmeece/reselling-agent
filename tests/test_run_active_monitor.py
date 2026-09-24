"""run_active_monitor, sale-aware (mocked sheet / browser / scraper / alerts).

Energy Shot (4000099948): regular $39.99, on sale $31.99. Sale start must write G/AW/X and
NOT flag col P or alert; the reverse ($31.99 -> $39.99) must flag P and send the urgent alert."""
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from agents import scheduler as sch

COL = sch.load_col_map()
CONFIG = {"business": {"price_change_threshold": 0.50, "min_margin_threshold": 0.10,
                       "min_demand_score": 5, "sale_warn_hours": 48, "sale_urgent_hours": 24},
          "categories": {}}
START_ROW = 4


def _row(**cells):
    row = []
    for key, val in cells.items():
        idx = sch.col_to_idx(COL[key])
        while len(row) <= idx:
            row.append("")
        row[idx] = val
    return row


def _energy_row(cost, badge="", regular="", flag="", ebay_price="59.99"):
    return _row(status="ACTIVE", title="Kirkland Signature Energy Shot", category="Pharmacy",
                costco_url="https://www.costco.com/x.product.1711796.html",
                costco_cost=str(cost), ebay_price=ebay_price, fee_rate="13.25%", ship_cost="0",
                demand_score="8", sale_info=badge, regular_price=regular, price_change=flag,
                stock_status="In Stock")


def _scrape(price, on_sale, original=None, savings=None, expires=None,
            coupon_type=None, coupon_label=None):
    return {"price": price, "stock_status": "In Stock", "image_urls": ["http://img/1.jpg"],
            "on_sale": on_sale, "original_price": original, "sale_savings": savings,
            "sale_expires": expires, "coupon_type": coupon_type, "coupon_label": coupon_label,
            "error": None}


@pytest.fixture
def harness(monkeypatch):
    calls = {"writes": [], "urgent": [], "expiry": [], "sales": [], "recorded": []}
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

    def run(rows, scrape, only_rows=None):
        for key in ("writes", "urgent", "expiry", "sales", "recorded"):
            calls[key].clear()
        state["rows"], state["scrape"] = rows, scrape
        sch.run_active_monitor(CONFIG, COL, object(), "Product Tracker", START_ROW, 500,
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
    future = (datetime.now() + timedelta(days=20)).strftime("%m/%d/%y")
    calls = harness([_energy_row(31.99, badge=f"🔥 -$8 ends {future}", regular="39.99", ebay_price="41.48")],
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
    assert COL["price_change"] not in w                       # untouched, no longer blanked
    calls = harness([_energy_row(39.99, flag="YES — update listing", ebay_price="79.99")],
                    _scrape(39.99, False))
    _r, w2 = _written(calls)
    assert w2[COL["price_change"]] == ""                      # listing fixed -> flag cleared


def test_expiry_countdown_fires_from_freshly_written_sale_data(harness):
    end = datetime.now() + timedelta(hours=30)
    exp = f"{end.month}/{end.day}/{end:%y}"
    calls = harness([_energy_row(39.99)],
                    _scrape(31.99, True, original=39.99, savings=8.0, expires=exp))
    # the row had no badge in the sheet before this run; the badge written THIS run is counted
    assert len(calls["expiry"]) == 1
    products, _hours = calls["expiry"][0]
    assert products[0]["sale_expires"] == exp and products[0]["regular_costco_cost"] == 39.99
    assert calls["recorded"][0].endswith("|warn") or calls["recorded"][0].endswith("|urgent")


def test_expiry_urgent_tier_inside_24h(harness):
    end = datetime.now() + timedelta(hours=1)
    exp = f"{end.month}/{end.day}/{end:%y}"     # 23:59 today: always < 24h away
    calls = harness([_energy_row(31.99, badge=f"🔥 -$8 ends {exp}", regular="39.99")],
                    _scrape(31.99, True, original=39.99, savings=8.0, expires=exp))
    assert calls["recorded"][0].endswith("|urgent")


def test_only_rows_limits_scrape_and_alerts(harness):
    rows = [_energy_row(39.99), _energy_row(39.99)]
    calls = harness(rows, _scrape(31.99, True, original=39.99, savings=8.0, expires="10/18/26"),
                    only_rows={START_ROW + 1})
    assert [r for r, _w in calls["writes"]] == [START_ROW + 1]
