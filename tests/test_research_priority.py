"""Research-queue ordering: on-sale rows (col X, index 23) first, soonest end first."""

from datetime import date

import pytest

from agents.researcher import _sale_end_sort_key

TODAY = date(2026, 10, 9)


def _row(name, badge=None):
    row = ["PENDING", "", name, "Pharmacy"] + [""] * 20
    if badge is not None:
        row[23] = badge
    else:
        row = row[:23]          # short row: col X absent entirely (Sheets trims trailing blanks)
    return row


def _order(rows):
    queue = [(i + 4, r) for i, r in enumerate(rows)]
    queue.sort(key=lambda t: _sale_end_sort_key(t[1], today=TODAY))
    return [r[2] for _, r in queue]


def test_dated_sale_sorts_before_non_sale():
    assert _order([_row("plain", ""), _row("sale", "🔥 -$8 ends 10/18/26")]) == ["sale", "plain"]


def test_dated_sales_sort_by_end_ascending():
    rows = [_row("late", "🔥 -$5 ends 10/25/26"), _row("soon", "🔥 -$8 ends 10/18/26")]
    assert _order(rows) == ["soon", "late"]


def test_undated_badge_between_dated_and_non_sale():
    rows = [_row("plain", ""), _row("undated", "🔥 -$8"), _row("dated", "🔥 -$3 ends 10/20/26")]
    assert _order(rows) == ["dated", "undated", "plain"]


def test_blank_col_x_sorts_last_and_keeps_sheet_order():
    rows = [_row("a", ""), _row("b"), _row("sale", "ends 10/12"), _row("c", "   "), _row("d")]
    assert _order(rows) == ["sale", "a", "b", "c", "d"]


def test_expired_badge_is_treated_as_non_sale():
    rows = [_row("a", ""), _row("expired", "🔥 -$8 ends 9/30/26")]
    assert _order(rows) == ["a", "expired"]


@pytest.mark.parametrize("bad", [None, 12.5, "ends 13/45/26", "ends 2/30", object(), "🔥🔥🔥", "ends //"])
def test_corrupt_col_x_never_raises(bad):
    row = _row("x", "")
    row[23] = bad
    key = _sale_end_sort_key(row, today=TODAY)
    assert key[0] in (1, 2)


def test_non_list_row_never_raises():
    assert _sale_end_sort_key(None) == (2, "")
