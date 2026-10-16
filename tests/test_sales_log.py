"""Tests for tools/sales_log.py — the per-transaction eBay sales log (data/sales_log.json)."""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sales_log as sl

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _txn(tid, created, price=100.0, fee=15.0, qty=1):
    return {"transaction_id": tid, "price": price, "fee": fee, "quantity": qty, "created": created}


def test_load_missing_or_corrupt_file_is_empty(tmp_path):
    assert sl.load(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert sl.load(str(bad)) == {}


def test_add_transactions_computes_net_and_dedupes(tmp_path):
    log = {}
    # cost 50 -> net/unit = 100 - 15 - 50*1.0825 = 30.875 -> 30.88
    added = sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00.000Z")], 50.0)
    assert added == 1
    (e,) = log["111"]
    assert e == {"transaction_id": "A", "created": "2026-10-07T10:00:00.000Z", "quantity": 1,
                 "price": 100.0, "fee": 15.0, "net": 30.88}
    # same transaction again (next run) -> nothing added, net frozen even if cost moved
    assert sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00.000Z")], 80.0) == 0
    assert log["111"][0]["net"] == 30.88


def test_multi_quantity_net_is_per_unit_times_quantity():
    log = {}
    # 2 units at $100, fee $30 total, cost 50 -> per unit 100 - 15 - 54.125 = 30.875 -> 30.88 x2
    sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00Z", fee=30.0, qty=2)], 50.0)
    assert log["111"][0]["net"] == 61.76               # actual_net_per_unit (rounded) x qty


def test_unknown_cost_logs_sale_with_null_net():
    log = {}
    sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00Z")], None)
    assert log["111"][0]["net"] is None and log["111"][0]["quantity"] == 1


def test_transaction_without_id_dedupes_on_created_and_price():
    log = {}
    t = _txn("", "2026-10-07T10:00:00Z")
    assert sl.add_transactions(log, "111", [t], 50.0) == 1
    assert sl.add_transactions(log, "111", [dict(t)], 50.0) == 0


def test_save_round_trip_is_atomic(tmp_path):
    p = str(tmp_path / "sub" / "sales_log.json")
    log = {}
    sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00Z")], 50.0)
    sl.save(log, p)
    assert sl.load(p) == log
    assert not [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")]


def test_summary_totals_and_windows():
    log = {}
    sl.add_transactions(log, "111", [
        _txn("A", "2026-10-10T00:00:00Z"),            # 6 days ago   -> 15/30/60/90
        _txn("B", "2026-09-26T00:00:00Z"),            # 20 days ago  -> 30/60/90
        _txn("C", "2026-08-27T00:00:00Z"),            # 50 days ago  -> 60/90
        _txn("D", "2026-06-01T00:00:00Z"),            # 137 days ago -> lifetime only
    ], 50.0)
    sl.add_transactions(log, "222", [_txn("E", "2026-10-15T00:00:00Z")], None)   # no cost
    s = sl.summary(log, now=NOW)
    assert s["units"] == 5 and s["unknown"] == 1 and s["sales"] == 5
    assert s["net"] == round(4 * 30.88, 2)
    assert s["windows"] == {15: 30.88, 30: 61.76, 60: 92.64, 90: 92.64}


def test_summary_of_empty_log():
    s = sl.summary({}, now=NOW)
    assert s["units"] == 0 and s["net"] == 0.0 and s["sales"] == 0


def test_unparseable_created_counts_in_lifetime_only():
    log = {"111": [{"transaction_id": "A", "created": "garbage", "quantity": 1,
                    "price": 1.0, "fee": 0.0, "net": 5.0}]}
    s = sl.summary(log, now=NOW)
    assert s["net"] == 5.0 and s["windows"][90] == 0.0


def test_has_item():
    log = {}
    assert not sl.has_item(log, "111")
    sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00Z")], 50.0)
    assert sl.has_item(log, "111")
    json.dumps(log)                                     # stays JSON-serialisable


def test_rotation_engine_ignores_absurd_units_sold():
    from tools.rotation_engine import _parse_units
    assert _parse_units("6") == 6 and _parse_units("") == 0
    assert _parse_units("100407064") == 0 and _parse_units("-2") == 0


def test_unknown_net_is_filled_in_later_but_known_net_stays_frozen():
    log = {}
    sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00Z")], None)     # no cost yet
    assert log["111"][0]["net"] is None
    assert sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00Z")], 50.0) == 0
    assert log["111"][0]["net"] == 30.88                    # upgraded in place, not duplicated
    sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00Z")], 80.0)
    assert log["111"][0]["net"] == 30.88                    # known net never rewritten


def test_estimated_fee_is_flagged_and_replaced_by_a_real_one():
    log = {}
    est = {**_txn("A", "2026-10-07T10:00:00Z", fee=12.0), "fee_estimated": True}
    sl.add_transactions(log, "111", [est], 50.0)
    assert log["111"][0]["fee_estimated"] is True
    assert sl.summary(log, now=NOW)["estimated"] == 1
    sl.add_transactions(log, "111", [_txn("A", "2026-10-07T10:00:00Z", fee=15.0)], 50.0)   # real fee
    e = log["111"][0]
    assert e["fee"] == 15.0 and e["net"] == 30.88 and not e.get("fee_estimated")


def test_needs_backfill_until_every_net_is_known():
    log = {}
    assert sl.needs_backfill(log, "111")
    sl.add_transactions(log, "111", [_txn("A", "2026-08-16T10:00:00Z")], None)
    assert sl.needs_backfill(log, "111")                 # old sale with unknown net -> 90 days again
    sl.add_transactions(log, "111", [_txn("A", "2026-08-16T10:00:00Z")], 50.0)
    assert not sl.needs_backfill(log, "111")


# ── refunds / cancellations + calendar-month view (2026-10-16 reconciliation) ──

def _rtxn(tid, created, price=100.0, fee=15.0, qty=1, refunded=0.0, shipped=True):
    return {**_txn(tid, created, price, fee, qty), "refunded": refunded, "shipped": shipped}


def test_refunded_unshipped_order_is_cancelled_and_excluded():
    """GreenPan 10/15: paid $103.99, refunded $88.07 three hours later, never shipped."""
    log = {}
    sl.add_transactions(log, "111", [_rtxn("A", "2026-10-10T00:00:00Z"),
                                     _rtxn("B", "2026-10-15T11:44:35Z", refunded=88.07, shipped=False)], 50.0)
    assert log["111"][1]["cancelled"] is True and log["111"][1]["refunded"] == 88.07
    s = sl.summary(log, now=NOW)
    assert s["units"] == 1 and s["orders"] == 1 and s["cancelled"] == 1
    assert s["net"] == 30.88


def test_partial_refund_on_shipped_order_reduces_net_but_keeps_the_sale():
    log = {}
    sl.add_transactions(log, "111", [_rtxn("A", "2026-10-10T00:00:00Z", refunded=10.0)], 50.0)
    assert not log["111"][0]["cancelled"]
    s = sl.summary(log, now=NOW)
    assert s["units"] == 1 and s["net"] == 20.88


def test_full_refund_on_shipped_order_is_cancelled():
    log = {}
    sl.add_transactions(log, "111", [_rtxn("A", "2026-10-10T00:00:00Z", refunded=100.0)], 50.0)
    assert log["111"][0]["cancelled"] is True


def test_refund_arriving_later_updates_a_logged_sale():
    log = {}
    sl.add_transactions(log, "111", [_rtxn("A", "2026-10-15T00:00:00Z", shipped=False)], 50.0)
    assert not log["111"][0]["cancelled"]
    sl.add_transactions(log, "111", [_rtxn("A", "2026-10-15T00:00:00Z", refunded=88.07, shipped=False)], 50.0)
    assert log["111"][0]["cancelled"] is True and len(log["111"]) == 1
    assert log["111"][0]["net"] == 30.88                     # frozen sale net kept; summary excludes it


def test_old_entries_without_refund_fields_still_count():
    log = {"111": [{"transaction_id": "A", "created": "2026-10-10T00:00:00Z", "quantity": 2,
                    "price": 10.0, "fee": 1.0, "net": 4.0}]}
    s = sl.summary(log, now=NOW)
    assert s["units"] == 2 and s["orders"] == 1 and s["net"] == 4.0


def test_months_are_calendar_months_in_central_time_newest_first():
    log = {}
    sl.add_transactions(log, "111", [
        _rtxn("A", "2026-08-16T16:13:41Z"),
        _rtxn("B", "2026-09-14T18:27:58Z"),
        _rtxn("C", "2026-10-01T03:00:00Z"),      # 9/30 10 PM Central -> September
        _rtxn("D", "2026-10-06T22:05:07Z", qty=3, fee=45.0),
        _rtxn("E", "2026-06-01T00:00:00Z"),      # outside 90 days
    ], 50.0)
    s = sl.summary(log, now=NOW)
    assert [(m["label"], m["units"], m["orders"]) for m in s["months"]] == [
        ("Oct", 3, 1), ("Sep", 2, 2), ("Aug", 1, 1)]
    assert s["months"][0]["net"] == round(3 * 30.88, 2)
    assert s["d90"] == {"units": 6, "orders": 4, "net": round(6 * 30.88, 2)}
    assert s["units"] == 7                                   # lifetime still includes June


def test_month_label_includes_year_when_not_current_year():
    log = {}
    sl.add_transactions(log, "111", [_rtxn("A", "2026-12-20T00:00:00Z")], 50.0)
    s = sl.summary(log, now=datetime(2027, 1, 10, tzinfo=timezone.utc))
    assert s["months"][0]["label"] == "Dec 2026"
