"""Stock-status classification for Costco product pages (pickup vs shipping).

Covers the pure _classify_stock decision table and _detect_shippable's page probes.
The bug fixed here: Costco shows the Add-to-Cart button as out-of-stock when the
*warehouse pickup* option is OOS, even though online shipping is still available —
which wrongly demoted live listings to PAUSED_OOS.
"""
from tools.costco_scraper import _classify_stock, _detect_shippable


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
