"""ebay_sync "no Costco in titles" sweep (spec 2026-10-26-ebay-title-costco-sweep)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import ebay_sync  # noqa: E402

COL = {"seo_title": "AI"}


@pytest.mark.parametrize("before,after", [
    ("Grand Leisure Las Palmas 6-Piece Outdoor Patio Seating Set - Costco Quality",
     "Grand Leisure Las Palmas 6-Piece Outdoor Patio Seating Set"),
    ("Kirkland Signature Calcium 600 mg Costco", "Kirkland Signature Calcium 600 mg"),
    ("Organic Soy Beverage Costco Non-Dairy", "Organic Soy Beverage Non-Dairy"),
    ("SunVilla Sara – Costco", "SunVilla Sara"),
    ("Protein Bar Variety – Costco Brand", "Protein Bar Variety"),
    ("Transformer Table Costco 2024", "Transformer Table"),
    ("Multivitamin Gummies Costco Formula", "Multivitamin Gummies"),
    ("Kirkland Signature Organic Quinoa", "Kirkland Signature Organic Quinoa"),
])
def test_strip_costco_title_ground_truth(before, after):
    assert ebay_sync._strip_costco_title(before) == after


def test_strip_costco_title_caps_at_max_len():
    assert len(ebay_sync._strip_costco_title("x" * 120 + " Costco")) == ebay_sync.MAX_TITLE_LEN


# ── sweep_titles ──────────────────────────────────────────────────────────────

MATCHED = "SunVilla Sara 5-Piece Outdoor Patio Seating Set – Costco"
ORPHAN = "Kirkland Wild Alaskan Fish Oil 1400mg 230 Softgels Omega-3 Costco"
CLEAN = "Kirkland Signature Organic Quinoa 4.5 lbs Bag Non-GMO"


def _listings(**over):
    base = [{"item_id": "318943101184", "title": MATCHED, "listing_type": "FixedPriceItem"},
            {"item_id": "318943101193", "title": ORPHAN, "listing_type": "FixedPriceItem"},
            {"item_id": "318925902429", "title": CLEAN, "listing_type": "FixedPriceItem"}]
    for item in base:
        item.update(over.get(item["item_id"], {}))
    return base


REPORT = {"matched": [{"item_id": "318943101184", "row_num": 30, "title": "SunVilla Sara 5-piece"},
                      {"item_id": "318925902429", "row_num": 25, "title": "Kirkland Quinoa"}]}


@pytest.fixture
def ebay(monkeypatch):
    calls = {"revise": [], "writes": [], "revise_ok": True}

    def _revise(item_id, title, listing_type=None):
        calls["revise"].append((item_id, title))
        return ({"ok": True, "error_kind": None, "message": ""} if calls["revise_ok"]
                else {"ok": False, "error_kind": "api", "error_code": "21919301", "message": "nope"})
    monkeypatch.setattr(ebay_sync, "revise_title", _revise)
    monkeypatch.setattr(ebay_sync, "write_row_partial",
                        lambda svc, sheet, row, pairs: calls["writes"].append((row, pairs)))
    monkeypatch.setattr(ebay_sync, "_sleep", lambda s: None)
    return calls


def _titles():
    return {30: "SunVilla Sara 5-piece", 25: "Kirkland Quinoa"}


def test_sweep_revises_orphan_and_matched_writes_sheet_only_for_matched(ebay):
    res = ebay_sync.sweep_titles(_listings(), REPORT, "svc", COL, "Product Tracker",
                                 dry_run=False, title_reader=_titles)
    assert ebay["revise"] == [("318943101184", "SunVilla Sara 5-Piece Outdoor Patio Seating Set"),
                              ("318943101193", "Kirkland Wild Alaskan Fish Oil 1400mg 230 Softgels Omega-3")]
    assert ebay["writes"] == [(30, [("AI", "SunVilla Sara 5-Piece Outdoor Patio Seating Set")])]
    assert [(r["item_id"], r.get("row_num"), r["outcome"]) for r in res] == [
        ("318943101184", 30, "revised"), ("318943101193", None, "revised")]


def test_clean_title_is_a_no_op(ebay):
    res = ebay_sync.sweep_titles([_listings()[2]], REPORT, "svc", COL, "Product Tracker",
                                 dry_run=False, title_reader=_titles)
    assert res == [] and ebay["revise"] == [] and ebay["writes"] == []


def test_dry_run_returns_plan_with_zero_writes(ebay):
    res = ebay_sync.sweep_titles(_listings(), REPORT, "svc", COL, "Product Tracker",
                                 dry_run=True, title_reader=_titles)
    assert ebay["revise"] == [] and ebay["writes"] == []
    assert [r["outcome"] for r in res] == ["dry_run", "dry_run"]
    assert res[0]["clean_title"] == "SunVilla Sara 5-Piece Outdoor Patio Seating Set"


def test_moved_row_is_revised_on_ebay_but_sheet_not_written(ebay):
    res = ebay_sync.sweep_titles(_listings(), REPORT, "svc", COL, "Product Tracker",
                                 dry_run=False, title_reader=lambda: {30: "Something Else"})
    assert ebay["writes"] == []
    assert res[0]["outcome"] == "revised" and "row moved" in res[0]["error"]


def test_failed_revise_writes_nothing_and_is_reported(ebay):
    ebay["revise_ok"] = False
    res = ebay_sync.sweep_titles(_listings(), REPORT, "svc", COL, "Product Tracker",
                                 dry_run=False, title_reader=_titles)
    assert ebay["writes"] == [] and [r["outcome"] for r in res] == ["failed", "failed"]
    assert "21919301" in res[0]["error"]


def test_non_fixed_price_listing_is_skipped(ebay):
    res = ebay_sync.sweep_titles(_listings(**{"318943101184": {"listing_type": "Chinese"}}), REPORT,
                                 "svc", COL, "Product Tracker", dry_run=False, title_reader=_titles)
    assert [r["item_id"] for r in res] == ["318943101193"]


def test_disabled_does_nothing(ebay):
    assert ebay_sync.sweep_titles(_listings(), REPORT, "svc", COL, "Product Tracker", dry_run=False,
                                  title_reader=_titles, enabled=False) == []
    assert ebay["revise"] == []


# ── format_title_sweep_summary ────────────────────────────────────────────────

def test_summary_silent_for_dry_run_and_clean():
    assert ebay_sync.format_title_sweep_summary([]) is None
    assert ebay_sync.format_title_sweep_summary([{"item_id": "1", "outcome": "dry_run"}]) is None


def test_summary_counts_revised_and_lists_failures():
    text = ebay_sync.format_title_sweep_summary([
        {"item_id": "1", "outcome": "revised"}, {"item_id": "2", "outcome": "revised"},
        {"item_id": "318943101193", "outcome": "failed", "error": "api: <boom>"}])
    assert text.startswith('🔤 <b>Title sweep:</b> 2 listing(s) revised (removed "Costco")')
    assert "item 318943101193" in text and "&lt;boom&gt;" in text


def test_active_list_request_asks_for_listing_type():
    xml = ebay_sync._build_request_xml("tok", 1)
    assert "<OutputSelector>ActiveList.ItemArray.Item.ListingType</OutputSelector>" in xml
