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
