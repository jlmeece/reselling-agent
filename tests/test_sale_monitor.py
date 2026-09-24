"""Pure sale-monitor logic (tools/sale_monitor.py): cost-event classification, badge/expiry
text, sale-end reprice alert, col P preservation, expiry dedup. No sheet / network."""
from datetime import datetime

import pytest

from tools.sale_monitor import (
    PRICE_FLAG_YES, already_alerted, classify_cost_event, expiry_tier, load_alert_state,
    margin_note_sale_start, parse_rate, parse_sale_expiry, price_flag_still_needed,
    record_alerts, sale_badge, sale_end_alert, to_float,
)
from tools.status_logic import suggest_reprice

TH = 0.50


@pytest.mark.parametrize("old,new,on_sale,badge,expected", [
    (39.99, 31.99, True, False, "sale_start"),      # Energy Shot goes on sale
    (39.99, 31.99, True, True, "sale_start"),       # deeper sale
    (31.99, 39.99, False, True, "sale_end"),        # the simulated rise
    (31.99, 39.99, False, False, "sale_end"),       # plain hike: margin shrank all the same
    (31.99, 35.99, True, True, "sale_end"),         # sale shrank but still on: cost rose
    (39.99, 39.99, False, True, "none"),            # stale badge, no cost move -> not an event
    (39.99, 40.20, False, False, "drift"),          # trivial
    (39.99, 39.70, False, False, "drift"),
    (39.99, 30.00, False, False, "drift"),          # non-sale drop: silent col G
    (39.99, 39.60, True, False, "drift"),           # tiny drop even though flagged on sale
    (39.99, 40.10, False, True, "sale_end"),        # badge gone + any rise
    (None, 31.99, True, False, "none"),
    (39.99, None, False, False, "none"),
])
def test_classify_cost_event(old, new, on_sale, badge, expected):
    assert classify_cost_event(old, new, on_sale, badge, TH) == expected


def test_sale_badge_and_margin_note():
    assert sale_badge(8.0, "10/18/26") == "🔥 -$8 ends 10/18/26"
    assert sale_badge(None, None) == "🔥 SALE"
    assert margin_note_sale_start(39.99, 31.99) == "on sale — margin +$8.00"


def test_simulated_31_99_to_39_99_rise_gives_urgent_reprice_up_alert():
    event = classify_cost_event(31.99, 39.99, False, True, TH)
    assert event == "sale_end"
    item = sale_end_alert("Kirkland Energy Shot", 31.99, 39.99, 0.1325, 0.0, 41.48,
                          row=9, category="Pharmacy")
    target = suggest_reprice(39.99, 0.1325, 0.0)
    assert target and target > 41.48
    assert "sale ended" in item["reason"]
    assert "$31.99→$39.99" in item["reason"]
    assert f"${target:.2f}" in item["reason"]
    assert f"${target:.2f}" in item["reprice_note"]
    assert item["row"] == 9 and item["title"] == "Kirkland Energy Shot"
    # at the current $41.48 eBay price the net is now negative-ish — the note says so
    assert "net is now" in item["reprice_note"]


def test_sale_end_alert_without_profitable_price_says_end_listing():
    item = sale_end_alert("x", 10.0, 900.0, 0.85, 0.0, 20.0)   # fee+margin >= 100% -> no target
    assert "end" in item["reason"].lower() and item["target"] is None


def test_sale_end_alert_unknown_fee_never_guesses_a_price():
    item = sale_end_alert("x", 10.0, 20.0, None, 0.0, 30.0)
    assert item["target"] is None


def test_price_flag_survives_until_listing_is_fixed():
    assert price_flag_still_needed(PRICE_FLAG_YES, 41.48, 59.99) is True
    assert price_flag_still_needed(PRICE_FLAG_YES, 59.99, 59.99) is False    # repriced
    assert price_flag_still_needed(PRICE_FLAG_YES, None, 59.99) is True      # never clear on missing data
    assert price_flag_still_needed(PRICE_FLAG_YES, 41.48, None) is True
    assert price_flag_still_needed("", 41.48, 59.99) is False
    assert price_flag_still_needed(None, 41.48, 59.99) is False


def test_parse_sale_expiry_formats():
    now = datetime(2026, 9, 24, 12, 0)
    assert parse_sale_expiry("🔥 -$8 ends 10/18/26", now) == datetime(2026, 10, 18, 23, 59)
    assert parse_sale_expiry("ends 10/18/2026", now) == datetime(2026, 10, 18, 23, 59)
    assert parse_sale_expiry("🔥 -$8 ends 10/18", now) == datetime(2026, 10, 18, 23, 59)   # year-less
    assert parse_sale_expiry("ends 1/5", now) == datetime(2027, 1, 5, 23, 59)              # rolls to next year
    assert parse_sale_expiry("🔥 SALE", now) is None
    assert parse_sale_expiry("ends 13/45/26", now) is None
    assert parse_sale_expiry("", now) is None


def test_expiry_tiers():
    assert expiry_tier(60, 48, 24) is None
    assert expiry_tier(40, 48, 24) == "warn"
    assert expiry_tier(10, 48, 24) == "urgent"
    assert expiry_tier(0, 48, 24) is None and expiry_tier(-5, 48, 24) is None


def test_alert_state_dedup_and_pruning(tmp_path):
    path = tmp_path / "state.json"
    assert load_alert_state(path) == {}
    state = record_alerts({}, ["a|2026-10-18|warn"], path=path, now=datetime(2026, 10, 16, 8, 0))
    assert already_alerted(load_alert_state(path), "a|2026-10-18|warn")
    assert not already_alerted(load_alert_state(path), "a|2026-10-18|urgent")   # next tier still fires
    state = record_alerts(state, ["b|x|warn"], path=path, now=datetime(2026, 12, 1))
    assert "a|2026-10-18|warn" not in state                                       # >30 days old, pruned


def test_helpers():
    assert to_float("$1,299.99") == 1299.99 and to_float("") is None and to_float(None) is None
    assert parse_rate("13.25%") == pytest.approx(0.1325)
    assert parse_rate(0.1325) == pytest.approx(0.1325)
    assert parse_rate("13.25") == pytest.approx(0.1325)
    assert parse_rate("") is None
