"""Costco price API parsing (display-price-lite, Sep 2026 redesign). No network:
fixtures are the real responses captured 2026-09-24."""
import json
import os

import pytest

from tools.costco_scraper import _parse_price_payload, price_miss_message

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


ENERGY = _load("costco_display_price_lite_energy_shot_sale.json")          # item 1711796
EXTRA = _load("costco_display_price_lite_extra_strength_sale.json")        # item 1711799


def test_real_sale_response_store_warehouse_first():
    # URL was whsNumber=847,1 -> the store (847): $39.99 regular, $8 off, $31.99 to pay
    p = _parse_price_payload(ENERGY, ["847", "1"])
    assert p == {"price": 31.99, "original_price": 39.99, "savings": 8.0,
                 "authoritative": True, "item_id": "1711796"}


def test_warehouse_1_is_a_different_undiscounted_price():
    p = _parse_price_payload(ENERGY, ["1", "847"])
    assert p["price"] == 34.99 and p["original_price"] is None and p["savings"] is None


def test_second_real_response_other_item():
    p = _parse_price_payload(EXTRA, ["847", "1"])
    assert (p["price"], p["original_price"], p["savings"], p["item_id"]) == (35.99, 43.99, 8.0, "1711799")
    assert _parse_price_payload(EXTRA, ["1", "847"])["price"] == 30.99


def test_no_warehouse_hint_uses_first_entry():
    assert _parse_price_payload(ENERGY)["price"] == 34.99          # first entry is wh "1"
    # a hint naming a warehouse that isn't present falls back to the first entry too
    assert _parse_price_payload(ENERGY, ["999"])["price"] == 34.99


def _payload(**entry):
    return {"priceData": [{"id": "1", "displayPrice": [{"warehouseNumber": "847", **entry}]}]}


def test_not_on_sale():
    p = _parse_price_payload(_payload(onlinePrice=24.99, aggregatedDiscountAmt=0, deliveredPrice=24.99), ["847"])
    assert p == {"price": 24.99, "original_price": None, "savings": None,
                 "authoritative": True, "item_id": "1"}


def test_strings_dollar_signs_and_commas():
    p = _parse_price_payload(_payload(onlinePrice="$1,299.99", aggregatedDiscountAmt="100.00",
                                      deliveredPrice="1,199.99"), ["847"])
    assert (p["price"], p["original_price"], p["savings"]) == (1199.99, 1299.99, 100.0)


def test_delivered_price_that_disagrees_with_online_minus_discount_is_not_trusted():
    # delivered folds shipping in (44.99 vs 39.99-8): keep online - discount so col G is not double-counted
    p = _parse_price_payload(_payload(onlinePrice=39.99, aggregatedDiscountAmt=8.0, deliveredPrice=44.99), ["847"])
    assert p["price"] == 31.99


def test_price_derived_when_delivered_missing_and_online_only():
    assert _parse_price_payload(_payload(onlinePrice=39.99, aggregatedDiscountAmt=8.0), ["847"])["price"] == 31.99
    assert _parse_price_payload(_payload(onlinePrice=12.5), ["847"])["price"] == 12.5
    assert _parse_price_payload(_payload(deliveredPrice=9.0), ["847"])["price"] == 9.0


def test_legacy_shapes_still_work_but_are_not_authoritative():
    p = _parse_price_payload({"finalOnlinePrice": "59.99"})
    assert p["price"] == 59.99 and p["authoritative"] is False     # DOM sale patterns may still run
    legacy = {"priceData": {"displayPrice": {"onlinePrice": 39.99}}}
    p = _parse_price_payload(legacy)
    assert p["price"] == 39.99 and p["authoritative"] is False


def test_no_discount_response_is_authoritative_so_dom_banners_cannot_fake_a_sale():
    # Real vitamin C response (item 98268, wh 847): $19.99, no discount. The page also shows
    # "Save $100" banners for OTHER products, which used to be read as this item's sale.
    p = _parse_price_payload(_payload(onlinePrice=19.99, aggregatedDiscountAmt=0.0, deliveredPrice=19.99), ["847"])
    assert p["authoritative"] is True and p["savings"] is None


@pytest.mark.parametrize("bad", [
    None, [], "x", {}, {"priceData": []}, {"priceData": [{}]}, {"priceData": [{"displayPrice": []}]},
    {"priceData": [{"displayPrice": [{"onlinePrice": 0}]}]},
    {"priceData": [{"displayPrice": [{"onlinePrice": "n/a"}]}]},
    {"priceData": "oops"}, {"finalOnlinePrice": 0},
])
def test_garbage_and_missing_fields_give_none(bad):
    assert _parse_price_payload(bad, ["847"]) is None


def test_price_miss_message_thresholds():
    assert price_miss_message({"pages": 20, "misses": 2}) is None          # below the minimum
    assert price_miss_message({"pages": 100, "misses": 3}) is None         # 3% — a few blips
    assert "3 of 10" in price_miss_message({"pages": 10, "misses": 3})
    assert price_miss_message({"pages": 0, "misses": 0}) is None
