"""Telegram review queue: on-sale SCORED items first (soonest sale end), then monthly profit."""

from datetime import date

import pytest

from agents.telegram_bot import extract_review_queue
from tests.test_telegram_bot import _COL, _make_row
from tools.sale_priority import sale_end_sort_key

TODAY = date(2026, 10, 9)


def _item(title, net="$10.00", sold="3", badge=""):
    # sold_90d / 3 = units per month; monthly profit = net × that
    return _make_row(A="SCORED", C=title, I=net, K=sold, X=badge)


def _titles(rows):
    return [i["title"] for i in extract_review_queue(rows, _COL, data_start_row=4, today=TODAY)]


def test_on_sale_item_beats_higher_monthly_profit_non_sale():
    rows = [_item("big earner", net="$50.00", sold="60"),               # $1000/mo
            _item("on sale", net="$5.00", sold="3", badge="🔥 -$8 ends 10/18/26")]  # $5/mo
    assert _titles(rows) == ["on sale", "big earner"]


def test_two_on_sale_items_soonest_end_first():
    rows = [_item("ends 10/25", badge="🔥 -$5 ends 10/25/26"),
            _item("ends 10/18", badge="🔥 -$8 ends 10/18/26")]
    assert _titles(rows) == ["ends 10/18", "ends 10/25"]


def test_same_end_date_breaks_ties_by_monthly_profit():
    rows = [_item("small", net="$5.00", badge="🔥 -$8 ends 10/11/26"),
            _item("large", net="$40.00", badge="🔥 -$800 ends 10/11/26")]
    assert _titles(rows) == ["large", "small"]


def test_undated_badge_after_dated_before_non_sale():
    rows = [_item("plain", net="$90.00", sold="90"),
            _item("undated", badge="🔥 -$8"),
            _item("dated", badge="🔥 -$3 ends 10/20/26")]
    assert _titles(rows) == ["dated", "undated", "plain"]


def test_expired_badge_sorts_as_non_sale():
    rows = [_item("expired", net="$5.00", badge="🔥 -$8 ends 9/30/26"),
            _item("plain", net="$20.00")]
    assert _titles(rows) == ["plain", "expired"]


def test_non_sale_items_keep_monthly_profit_desc_then_net():
    rows = [_item("low", net="$4.00", sold="3"),        # $4/mo
            _item("high", net="$10.00", sold="30"),     # $100/mo
            _item("fast cheap", net="$10.00", sold="15"),  # $50/mo, $10/unit
            _item("slow rich", net="$50.00", sold="3")]    # $50/mo, $50/unit
    assert _titles(rows) == ["high", "slow rich", "fast cheap", "low"]


def test_full_ties_keep_sheet_order():
    rows = [_item("first"), _item("second"), _item("third")]
    assert _titles(rows) == ["first", "second", "third"]


def test_below_floor_still_excluded_even_when_on_sale():
    rows = [_item("cheap sale", net="$1.00", badge="🔥 -$8 ends 10/18/26"), _item("ok")]
    assert _titles(rows) == ["ok"]


@pytest.mark.parametrize("bad", [None, 12.5, "ends 13/45/26", "ends 2/30", "🔥🔥🔥", "ends //", object()])
def test_corrupt_sale_info_never_raises(bad):
    row = _item("x")
    row[23] = bad
    assert sorted(_titles([row, _item("y", net="$20.00")])) == ["x", "y"]   # no crash, nothing dropped
    assert sale_end_sort_key(bad, today=TODAY)[0] in (1, 2)                 # never "dated on-sale"


def test_short_row_without_col_x_is_non_sale():
    short = _item("short", net="$90.00", sold="90")[:20]
    rows = [short, _item("sale", badge="🔥 -$8 ends 10/18/26")]
    assert _titles(rows) == ["sale", "short"]
