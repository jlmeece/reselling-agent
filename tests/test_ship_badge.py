import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.researcher import _ship_badge


def test_free_shipping_returns_check_free():
    assert _ship_badge(free_shipping=True, cart_est={}) == "✓ FREE"


def test_free_shipping_wins_even_when_cart_has_cost():
    # free_shipping flag from product page overrides cart_est
    assert _ship_badge(free_shipping=True, cart_est={"shipping": 12.99}) == "✓ FREE"


def test_paid_shipping_shows_dollar_amount():
    assert _ship_badge(free_shipping=False, cart_est={"shipping": 12.99}) == "$12.99 ship"


def test_paid_shipping_zero_shows_zero():
    assert _ship_badge(free_shipping=False, cart_est={"shipping": 0.0}) == "$0.00 ship"


def test_no_cart_estimate_returns_blank():
    assert _ship_badge(free_shipping=False, cart_est={}) == ""


def test_cart_estimate_none_value_returns_blank():
    assert _ship_badge(free_shipping=False, cart_est={"shipping": None}) == ""
