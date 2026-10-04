"""Stock-status classification for Costco product pages (pickup vs shipping).

Covers the pure _classify_stock decision table and _detect_shippable's page probes.
The bug fixed here: Costco shows the Add-to-Cart button as out-of-stock when the
*warehouse pickup* option is OOS, even though online shipping is still available —
which wrongly demoted live listings to PAUSED_OOS.

Since 2026-10-04 the inventory API ("How to get it" box) is the authority: its DELIVERY
state decides and warehouse pickup is ignored; the ATC/page-text rules above are only the
fallback when no inventory call fired. Real responses: tests/fixtures/inventory_api_2026-10-04.json
(7 live products, probed with tools/probe_inventory.py).
"""
import json
import os

import pytest

from tools.costco_scraper import (
    _classify_stock, _delivery_state, _detect_shippable, _parse_inventory_payload,
)

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "inventory_api_2026-10-04.json")
with open(_FIXTURE, encoding="utf-8") as _f:
    LIVE = json.load(_f)


class FakeEl:
    def __init__(self, cls=""):
        self._cls = cls

    def get_attribute(self, name):
        return self._cls if name == "class" else None


class FakePage:
    """Maps a selector substring to the elements it 'matches'."""

    def __init__(self, by_selector=None):
        self.by_selector = by_selector or {}

    def query_selector_all(self, sel):
        for key, els in self.by_selector.items():
            if key in sel:
                return els
        return []


# ── _classify_stock: ATC button present ───────────────────────────────────────

def test_atc_out_of_stock_but_shippable_is_in_stock():
    # The core fix: pickup-OOS ATC button + a live shipping option = sellable.
    assert _classify_stock(True, "out-of-stock", "", None, shippable=True) == ("In Stock", True)


def test_atc_out_of_stock_and_not_shippable_is_out_of_stock():
    assert _classify_stock(True, "out-of-stock", "", None, shippable=False) == ("OUT OF STOCK", False)


def test_atc_out_of_stock_underscore_variant():
    assert _classify_stock(True, "out_of_stock", "", None, shippable=False) == ("OUT OF STOCK", False)


def test_atc_enabled_no_limit_is_in_stock():
    assert _classify_stock(True, "add-to-cart", "", None) == ("In Stock", True)


def test_atc_enabled_with_limit_shows_number():
    assert _classify_stock(True, "", "", 3) == ("Available (3/day limit)", True)


def test_atc_enabled_limited_text():
    assert _classify_stock(True, "", "while supplies last", None) == ("Available (limited)", True)


# ── _classify_stock: no ATC button (body-text fallback) ───────────────────────

def test_no_atc_warehouse_only():
    assert _classify_stock(False, "", "available in club only", None) == ("WAREHOUSE ONLY", False)


def test_no_atc_out_of_stock_not_shippable():
    assert _classify_stock(False, "", "out of stock", None, shippable=False) == ("OUT OF STOCK", False)


def test_no_atc_out_of_stock_but_shippable_is_in_stock():
    assert _classify_stock(False, "", "out of stock", None, shippable=True) == ("In Stock", True)


def test_no_atc_unknown():
    assert _classify_stock(False, "", "", None) == ("Unknown", False)


# ── _detect_shippable ─────────────────────────────────────────────────────────

def test_detect_shippable_delivery_option_enabled():
    page = FakePage({"delivery": [FakeEl(cls="delivery-option")]})
    assert _detect_shippable(page, "") is True


def test_detect_shippable_skips_out_of_stock_option_then_uses_body_text():
    # A delivery option that is itself out-of-stock is skipped; fall back to body text.
    page = FakePage({"delivery": [FakeEl(cls="out-of-stock")]})
    assert _detect_shippable(page, "ships to you free") is True


def test_detect_shippable_body_text_ships_to():
    assert _detect_shippable(FakePage(), "ships to you") is True


def test_detect_shippable_body_text_warehouse_only_is_false():
    assert _detect_shippable(FakePage(), "available in club only, not available online") is False


def test_detect_shippable_nothing_is_false():
    assert _detect_shippable(FakePage(), "some unrelated text") is False


# ── Inventory API: real responses (7 live products, 2026-10-04) ───────────────

def _records(item):
    recs = [_parse_inventory_payload(r["url"], r["json"]) for r in LIVE[item]["responses"]]
    return [r for r in recs if r]


@pytest.mark.parametrize("item", sorted(LIVE))
def test_live_delivery_state_matches_how_to_get_it_box(item):
    state, _ = _delivery_state(_records(item))
    expected = "oos" if LIVE[item]["ui_delivery_out_of_stock"] else "available"
    assert state == expected, LIVE[item]["name"]


@pytest.mark.parametrize("item,status,in_stock", [
    ("4000099948", "Available (15/day limit)", True),   # Energy Shot — delivery in stock
    ("11487038",   "In Stock",                 True),   # Calcium — pickup NOSTOCK, delivery OK
    ("4000204559", "In Stock",                 True),   # Agio — pickup NOSTOCK, delivery OK (LTL)
    ("4000399020", "In Stock",                 True),   # dog food — 2-Day delivery
    ("4000391466", "OUT OF STOCK",             False),  # SunVilla — online-only, v2 NOSTOCK
    ("4000434781", "OUT OF STOCK",             False),  # POLYWOOD — v2 INSTOCK but ship mode 400
    ("4000359830", "OUT OF STOCK",             False),  # Ninja — v2 INSTOCK, isAvailable false,
])                                                      #   warehouse INSTOCK (ignored)
def test_live_truth_table_end_to_end(item, status, in_stock):
    delivery, _ = _delivery_state(_records(item))
    limit = 15 if item == "4000099948" else None
    # Old signals deliberately contradict: ATC present & enabled, page says "ships to you".
    assert _classify_stock(True, "add-to-cart", "ships to you", limit, shippable=True,
                           delivery=delivery) == (status, in_stock)


def test_pickup_state_is_reported_not_decisive():
    _, pickup = _delivery_state(_records("11487038"))
    assert pickup == "NOSTOCK"                            # Calcium: warehouse out…
    assert _delivery_state(_records("11487038"))[0] == "available"   # …still sellable


def test_ninja_needs_the_ajax_signal():
    # v2 alone says in stock — the AjaxSCInventoryUpdate isAvailable=false is what matches the UI.
    v2_only = [r for r in _records("4000359830") if r["kind"] == "delivery"]
    assert _delivery_state(v2_only)[0] == "available"
    assert _delivery_state(_records("4000359830"))[0] == "oos"


# ── _parse_inventory_payload / _delivery_state edge cases ────────────────────

V2 = "https://ecom-api.costco.com/ebusiness/inventory/v1/inventorylevels/availability/v2/123?x=1"
AJAX = "https://www.costco.com/AjaxSCInventoryUpdate?itemNumber=123&warehouseNo=847"
PICKUP = "https://ecom-api.costco.com/ebusiness/inventory/v1/inventorylevels/availability/pickup/123"
BATCH = "https://ecom-api.costco.com/ebusiness/inventory/v1/inventorylevels/availability/batch/v2"


def _v2(avail="INSTOCK", sale=True, modes=(("200 OK", None),), item="123"):
    return _parse_inventory_payload(V2, {
        "itemNumber": item, "availability": avail, "availableForSale": sale,
        "shipmodeDates": [{"status": st, "description": d} for st, d in modes]})


def test_batch_and_junk_are_ignored():
    assert _parse_inventory_payload(BATCH, [{"itemNumber": "1"}]) is None
    assert _parse_inventory_payload(V2, "not json") is None
    assert _parse_inventory_payload(V2, {"unrelated": 1}) is None
    assert _parse_inventory_payload(AJAX, {"itemNumber": "123"}) is None   # no isAvailable


def test_failed_ship_mode_records_its_reason():
    rec = _v2(modes=(("400 Bad Request", "Insufficient stock available"),))
    assert rec["ship_ok"] is False and rec["ship_error"] == "Insufficient stock available"


def test_any_working_ship_mode_is_enough():
    rec = _v2(modes=(("400 Bad Request", "nope"), ("200 OK", None)))
    assert _delivery_state([rec])[0] == "available"


def test_no_ship_modes_trusts_availability():
    assert _delivery_state([_v2(modes=())])[0] == "available"


def test_available_for_sale_false_is_oos_even_if_instock():
    assert _delivery_state([_v2(sale=False)])[0] == "oos"


@pytest.mark.parametrize("avail", ["BACKORDER", "BACKORDERED", "PRESELL"])
def test_backorder_and_presell_stay_sellable(avail):
    state, _ = _delivery_state([_v2(avail=avail)])
    assert state == "backorder"
    assert _classify_stock(False, "", "", None, delivery=state) == ("Available (backorder)", True)


def test_ajax_alone_decides_when_v2_missing():
    ok = _parse_inventory_payload(AJAX, {"itemNumber": "123", "isAvailable": True})
    bad = _parse_inventory_payload(AJAX, {"itemNumber": "123", "isAvailable": False})
    assert _delivery_state([ok])[0] == "available"
    assert _delivery_state([bad])[0] == "oos"


def test_ajax_for_a_different_item_is_ignored():
    other = _parse_inventory_payload(AJAX, {"itemNumber": "999", "isAvailable": False})
    assert _delivery_state([_v2(), other])[0] == "available"


def test_pickup_only_gives_no_delivery_state():
    pk = _parse_inventory_payload(PICKUP, {"itemNumber": "123", "warehouseAvailability": {
        "inWarehouse": {"availability": "INSTOCK"}, "3rdPartyDelivery": {"availability": "NOSTOCK"}}})
    assert _delivery_state([pk]) == (None, "INSTOCK")   # -> DOM fallback decides


def test_nothing_captured_is_none():
    assert _delivery_state([]) == (None, None)


# ── _classify_stock: delivery input outranks the DOM ─────────────────────────

def test_delivery_oos_beats_enabled_atc_button():
    assert _classify_stock(True, "add-to-cart", "", None, delivery="oos") == ("OUT OF STOCK", False)


def test_delivery_available_beats_out_of_stock_atc_and_unshippable_dom():
    assert _classify_stock(True, "out-of-stock", "", None, shippable=False,
                           delivery="available") == ("In Stock", True)


def test_delivery_available_keeps_limit_and_limited_labels():
    assert _classify_stock(False, "", "", 4, delivery="available") == ("Available (4/day limit)", True)
    assert _classify_stock(False, "", "low stock", None,
                           delivery="available") == ("Available (limited)", True)


def test_no_delivery_signal_falls_back_to_warehouse_only_text():
    # Delivery not offered at all (no inventory call) -> existing WAREHOUSE ONLY rule.
    assert _classify_stock(False, "", "available in club only", None,
                           delivery=None) == ("WAREHOUSE ONLY", False)


def test_inventory_miss_message_thresholds():
    from tools.costco_scraper import inventory_miss_message
    assert inventory_miss_message({"pages": 20, "misses": 2}) is None       # below the minimum
    assert inventory_miss_message({"pages": 100, "misses": 3}) is None      # a few blips
    assert "3 of 10" in inventory_miss_message({"pages": 10, "misses": 3})
    assert inventory_miss_message({"pages": 0, "misses": 0}) is None
