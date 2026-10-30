"""Orphan auto-link (col Q backfill) in tools/ebay_sync.py — sheet + eBay mocked, never live."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import ebay_sync
from tools.ebay_sync import alert_message, auto_link_orphans, summarize, sync

COL = {"status": "A", "title": "C", "platform": "E", "ebay_price": "H",
       "ebay_listing_url": "Q", "units_sold": "U", "sku": "AA", "seo_title": "AI"}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ebay_sync, "_sleep", lambda s: None)


def _row(n, title="Widget", status="READY", sku="", url="", seo="", platform=""):
    return {"row_num": n, "title": title, "status": status, "platform": platform,
            "ebay_price": "", "ebay_listing_url": url, "units_sold": "", "sku": sku,
            "seo_title": seo}


def _listing(item_id, title="Widget", sku="", qty=5):
    return {"item_id": item_id, "title": title, "sku": sku, "price": 29.99, "quantity": qty,
            "quantity_sold": 0, "watch_count": 0, "view_count": None}


@pytest.fixture
def sheet(monkeypatch):
    """rows: {row_num: row dict} served by _read_row; writes recorded."""
    state = {"rows": {}, "writes": []}
    monkeypatch.setattr(ebay_sync, "_read_row",
                        lambda svc, col, sheet_name, n: state["rows"].get(n))
    monkeypatch.setattr(ebay_sync, "safe_write_row",
                        lambda svc, sheet_name, n, pairs: state["writes"].append((n, dict(pairs))))
    return state


def _run(sheet, rows, listings, dry_run=False, sku_lookup=lambda iid: ""):
    sheet["rows"] = {r["row_num"]: dict(r) for r in rows}
    report = sync("svc", rows, listings, sheet_name="Product Tracker", col_map=COL,
                  cancelled_units={}, dry_run=dry_run)
    results = auto_link_orphans(report, rows, "svc", COL, "Product Tracker",
                                dry_run=dry_run, sku_lookup=sku_lookup)
    return report, results


def test_sku_match_writes_col_q_status_and_platform(sheet):
    rows = [_row(5, title="Costco name", sku="4000412053")]
    report, res = _run(sheet, rows, [_listing("860098461573", title="eBay SEO title", sku="4000412053")])
    assert sheet["writes"] == [(5, {"Q": "https://www.ebay.com/itm/860098461573",
                                    "A": "ACTIVE", "E": "eBay"})]
    assert res[0]["how"] == "sku" and res[0]["outcome"] == "linked"
    assert report["on_ebay_not_in_sheet"] == []                     # no longer an orphan
    assert "auto_linked 1" in summarize(report)
    msg = alert_message(report)
    assert "Auto-linked to the sheet" in msg and "linked to row 5 by SKU, READY → ACTIVE" in msg


def test_sku_lookup_used_when_activelist_has_no_sku(sheet):
    calls = []
    rows = [_row(7, title="Costco name", sku="111")]
    _r, res = _run(sheet, rows, [_listing("9001", title="Other words entirely")],
                   sku_lookup=lambda iid: calls.append(iid) or "111")
    assert calls == ["9001"] and res[0]["how"] == "sku"
    assert sheet["writes"][0][1]["Q"] == "https://www.ebay.com/itm/9001"


def test_exact_title_match_on_col_c_or_col_ai(sheet):
    rows = [_row(4, title="Ninja Foodi Oven"), _row(6, title="Costco long name", seo="Nuwave Pro XL Oven")]
    report, res = _run(sheet, rows, [_listing("1", title="ninja foodi oven"),
                                     _listing("2", title="Nuwave Pro XL Oven")])
    assert {(r["row_num"], r["item_id"], r["how"]) for r in res} == {(4, "1", "title"), (6, "2", "title")}
    assert {n for n, _ in sheet["writes"]} == {4, 6}


def test_close_title_match_is_only_suggested_never_written(sheet):
    rows = [_row(4, title="Ninja Foodi Smart Oven Black", status="ACTIVE")]
    report, res = _run(sheet, rows, [_listing("1", title="Ninja Foodi Smart Oven Black XL")])
    assert res == [] and sheet["writes"] == []
    assert report["on_ebay_not_in_sheet"][0]["match_kind"] == "close"   # still a suggestion


def test_never_overwrites_a_filled_col_q(sheet):
    # the row's col Q points at an ENDED listing; the new orphan has the same SKU
    rows = [_row(4, sku="111", url="https://www.ebay.com/itm/555", status="ACTIVE")]
    _r, res = _run(sheet, rows, [_listing("9001", sku="111")])
    assert res == [] and sheet["writes"] == []


def test_ambiguous_sku_or_title_links_nothing(sheet):
    # two rows share the SKU / two listings share a title
    rows = [_row(4, sku="111"), _row(5, sku="111")]
    assert _run(sheet, rows, [_listing("9001", sku="111", title="zzz")])[1] == []
    rows = [_row(4, title="Same Title")]
    assert _run(sheet, rows, [_listing("1", title="Same Title"), _listing("2", title="Same Title")])[1] == []
    assert sheet["writes"] == []


def test_title_match_refused_when_skus_disagree(sheet):
    rows = [_row(4, title="Gold Bracelet", sku="111")]
    assert _run(sheet, rows, [_listing("1", title="Gold Bracelet", sku="222")])[1] == []


def test_row_moved_or_col_q_filled_since_read_is_skipped(sheet):
    rows = [_row(4, sku="111"), _row(5, sku="222")]
    sheet_rows = {4: dict(rows[0], title="Something else"),
                  5: dict(rows[1], ebay_listing_url="https://www.ebay.com/itm/7")}

    def run():
        report = sync("svc", rows, [_listing("1", sku="111", title="a"), _listing("2", sku="222", title="b")],
                      sheet_name="Product Tracker", col_map=COL, cancelled_units={})
        sheet["rows"] = sheet_rows
        return report, auto_link_orphans(report, rows, "svc", COL, "Product Tracker",
                                         sku_lookup=lambda i: "")
    report, res = run()
    assert sheet["writes"] == []
    assert {r["row_num"]: r["error"] for r in res} == {4: "row moved", 5: "col Q filled since the read"}
    assert len(report["on_ebay_not_in_sheet"]) == 2                  # still orphans
    assert "NOT linked to row 4 (row moved)" in alert_message(report)


def test_dry_run_writes_nothing(sheet):
    report, res = _run(sheet, [_row(4, sku="111")], [_listing("1", sku="111")], dry_run=True)
    assert sheet["writes"] == [] and res[0]["outcome"] == "dry_run"
    assert "would link to row 4 by SKU" in alert_message(report)


def test_deliberate_status_kept_only_col_q_written(sheet):
    _r, res = _run(sheet, [_row(4, sku="111", status="AUDIT_REVIEW", platform="eBay")],
                   [_listing("1", sku="111")])
    assert sheet["writes"] == [(4, {"Q": "https://www.ebay.com/itm/1"})]
    assert res[0]["to_active"] is False


def test_unlinked_active_row_leaves_active_not_on_ebay(sheet):
    rows = [_row(4, sku="111", status="ACTIVE", platform="eBay")]
    report, _res = _run(sheet, rows, [_listing("1", sku="111", title="different")])
    assert report["active_not_on_ebay"] == []
    assert sheet["writes"] == [(4, {"Q": "https://www.ebay.com/itm/1"})]   # already ACTIVE


def test_disabled_does_nothing(sheet):
    rows = [_row(4, sku="111")]
    report = sync("svc", rows, [_listing("1", sku="111")], sheet_name="Product Tracker",
                  col_map=COL, cancelled_units={})
    assert auto_link_orphans(report, rows, "svc", COL, "Product Tracker", enabled=False) == []
    assert sheet["writes"] == [] and len(report["on_ebay_not_in_sheet"]) == 1
