"""Tests for tools/sale_history.py — the Sheets service is a fake, never live."""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sale_history
from tools.sale_history import log_sale, parse_sale_end, should_append

TODAY = date(2026, 9, 24)


# ── parse_sale_end ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("info,expected", [
    ("🔥 -$150 ends 5/31/26", "2026-05-31"),
    ("🔥 -$150 ends 5/31/2026", "2026-05-31"),
    ("🔥 SALE ends 10/5/26", "2026-10-05"),
    ("🔥 -$40 ends 9/30", "2026-09-30"),          # no year -> this year
    ("🔥 -$40 ends 1/5", "2027-01-05"),           # no year, already >30d past -> next year
    ("🔥 -$40 ends 9/1", "2026-09-01"),           # 23 days past -> still this year
    ("🔥 SALE", ""),
    ("", ""),
    (None, ""),
    ("ends 13/45/26", ""),                        # impossible date
])
def test_parse_sale_end(info, expected):
    assert parse_sale_end(info, TODAY) == expected


# ── should_append ─────────────────────────────────────────────────────────────

def _r(title="Vitamix", date_="2026-09-20", price="389.99"):
    return [title, "Small Appliances", date_, price, "449.99", "2026-09-30"]


def test_same_product_same_price_within_7_days_is_skipped():
    assert should_append([_r()], "Vitamix", 389.99, TODAY) is False
    assert should_append([_r(date_="2026-09-17")], "Vitamix", 389.99, TODAY) is False   # exactly 7d


def test_same_price_after_gap_appends():
    assert should_append([_r(date_="2026-09-16")], "Vitamix", 389.99, TODAY) is True    # 8d


def test_new_price_appends():
    assert should_append([_r()], "Vitamix", 379.99, TODAY) is True


def test_other_product_appends_and_title_match_is_case_insensitive():
    assert should_append([_r()], "Blendtec", 389.99, TODAY) is True
    assert should_append([_r()], " vitamix ", 389.99, TODAY) is False


def test_dedup_reads_formatted_prices_and_survives_bad_rows():
    rows = [["junk"], _r(price="$389.99"), _r(date_="not a date")]
    assert should_append(rows, "Vitamix", "$389.99", TODAY) is False
    assert should_append([_r(date_="garbage")], "Vitamix", 389.99, TODAY) is True
    assert should_append([], "Vitamix", 389.99, TODAY) is True


# ── log_sale with a fake service ──────────────────────────────────────────────

class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeService:
    def __init__(self, tabs=("Product Tracker",), rows=(), fail=False):
        self.tabs = list(tabs)
        self.rows = [list(r) for r in rows]
        self.fail = fail
        self.calls = []            # ("addSheet" | "header" | "append", payload)

    def spreadsheets(self):
        return self

    def get(self, **kw):
        if self.fail:
            return _Req(lambda: (_ for _ in ()).throw(RuntimeError("sheets down")))
        return _Req(lambda: {"sheets": [{"properties": {"title": t}} for t in self.tabs]})

    def batchUpdate(self, spreadsheetId, body):
        def go():
            title = body["requests"][0]["addSheet"]["properties"]["title"]
            self.tabs.append(title)
            self.calls.append(("addSheet", title))
        return _Req(go)

    def values(self):
        return self

    def update(self, **kw):
        return _Req(lambda: self.calls.append(("header", kw["body"]["values"][0])))

    def append(self, **kw):
        def go():
            self.rows.extend(kw["body"]["values"])
            self.calls.append(("append", kw["body"]["values"][0]))
        return _Req(go)


# values().get shares a name with spreadsheets().get, so the tab-list vs rows read is told
# apart by the presence of `range`.
class _Svc(FakeService):
    header = None      # what A1:H1 of an existing tab reads back (default: the current HEADER)

    def get(self, **kw):
        if "range" in kw:
            if str(kw["range"]).endswith("A1:H1"):
                hdr = sale_history.HEADER if self.header is None else self.header
                return _Req(lambda: {"values": [list(hdr)]})
            return _Req(lambda: {"values": self.rows})
        return super().get(**kw)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet123")
    monkeypatch.setattr(sale_history, "_header_checked", False)


def test_creates_tab_with_header_then_appends_row_with_blank_coupon_when_unknown():
    svc = _Svc()
    assert log_sale(svc, "Vitamix A3500", "Small Appliances", "$389.99", "449.99",
                    "🔥 -$60 ends 9/30/26", today=TODAY) is True
    assert [c[0] for c in svc.calls] == ["addSheet", "header", "append"]
    assert svc.calls[1][1] == sale_history.HEADER
    assert svc.calls[2][1] == ["Vitamix A3500", "Small Appliances", "2026-09-24", 389.99, 449.99, "2026-09-30", "", ""]


def test_blank_regular_price_writes_empty_string():
    svc = _Svc(tabs=("Sale History",))
    assert log_sale(svc, "Widget", "Toys", 19.99, "", "🔥 SALE", today=TODAY) is True
    assert svc.calls == [("append", ["Widget", "Toys", "2026-09-24", 19.99, "", "", "", ""])]


def test_existing_tab_is_not_recreated_and_duplicate_is_skipped():
    svc = _Svc(tabs=("Sale History",), rows=[_r("Widget", "2026-09-22", "19.99")])
    assert log_sale(svc, "Widget", "Toys", 19.99, "", "🔥 SALE", today=TODAY) is False
    assert svc.calls == []                                    # no addSheet, no append
    assert log_sale(svc, "Widget", "Toys", 14.99, "", "🔥 SALE", today=TODAY) is True   # new price
    assert svc.calls[0][0] == "append"


def test_not_on_sale_is_a_noop_that_never_touches_sheets():
    svc = _Svc()
    assert log_sale(svc, "Widget", "Toys", 19.99, "", "", today=TODAY) is False
    assert log_sale(svc, "Widget", "Toys", 19.99, "", None, today=TODAY) is False
    assert svc.calls == []


def test_failure_never_propagates():
    assert log_sale(_Svc(fail=True), "Widget", "Toys", 19.99, "", "🔥 SALE", today=TODAY) is False
    assert log_sale(object(), "Widget", "Toys", 19.99, "", "🔥 SALE", today=TODAY) is False


def test_coupon_type_and_label_are_logged():
    svc = _Svc(tabs=("Sale History",))
    assert log_sale(svc, "Energy Shot", "Pharmacy", 31.99, "39.99", "🔥 -$8 ends 10/18/26",
                    today=TODAY, coupon_type="MFR", coupon_label="Manufacturer Coupon") is True
    assert svc.calls == [("append", ["Energy Shot", "Pharmacy", "2026-09-24", 31.99, 39.99,
                                     "2026-10-18", "MFR", "Manufacturer Coupon"])]


def test_header_has_coupon_columns_in_order():
    assert sale_history.HEADER[-2:] == ["COUPON_TYPE", "COUPON_LABEL"]
    assert sale_history.HEADER[:6] == ["PRODUCT_TITLE", "CATEGORY", "SCRAPE_DATE", "SALE_PRICE",
                                       "REGULAR_PRICE", "SALE_END_DATE"]


def test_existing_six_column_tab_gets_header_upgraded_once_and_old_rows_untouched():
    old_row = ["Widget", "Toys", "2026-08-01", "19.99", "24.99", "2026-08-10"]     # blank coupon cells
    svc = _Svc(tabs=("Sale History",), rows=[old_row])
    svc.header = sale_history.HEADER[:6]
    assert log_sale(svc, "Gadget", "Toys", 9.99, "", "🔥 SALE", today=TODAY, coupon_type="STORE",
                    coupon_label="Instant Savings") is True
    assert svc.calls[0] == ("header", sale_history.HEADER)                            # G1:H1 topped up
    assert svc.calls[1][0] == "append"
    assert log_sale(svc, "Gizmo", "Toys", 5.99, "", "🔥 SALE", today=TODAY) is True
    assert [c[0] for c in svc.calls] == ["header", "append", "append"]               # once per process
    assert svc.rows[0] == old_row                                                     # no migration
