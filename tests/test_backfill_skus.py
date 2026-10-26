"""Tests for tools/backfill_skus.py — pure helpers only (no sheet I/O)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backfill_skus import extract_product_id  # noqa: E402


def test_extract_product_id():
    assert extract_product_id("https://www.costco.com/.product.1700000.html") == "1700000"
    assert extract_product_id("https://www.costco.com/.product.12345.html?langId=-1") == "12345"
    # modern /p/-/slug/<id> URLs (the form the old regex missed)
    assert extract_product_id("https://www.costco.com/p/-/gourmia-xl-13-in-1-air-fryer/4000195616") == "4000195616"
    assert extract_product_id("https://www.costco.com/p/-/some-slug/4000195616?langId=-1") == "4000195616"
    assert extract_product_id("https://www.costco.com/some-other-page.html") is None
    assert extract_product_id("") is None


# ── backfill_skus(service, sheet_name) — the daily-sweep entry point ─────────────────────

import pytest  # noqa: E402

from tools import sheet_writer  # noqa: E402
from tools.backfill_skus import backfill_skus, plan_assignments  # noqa: E402

COL = {"status": "A", "costco_url": "R", "sku": "AA"}


def _row(status="PENDING", url="", sku=""):
    r = [""] * 27
    r[0], r[17], r[26] = status, url, sku
    return r


@pytest.fixture
def sheet(monkeypatch):
    st = {"rows": [], "writes": [], "reads": 0, "after_first_read": None}

    def read(svc, rng):
        st["reads"] += 1
        if st["reads"] > 1 and st["after_first_read"] is not None:
            return st["after_first_read"]
        return [list(r) for r in st["rows"]]

    def write(svc, sheet_name, row_num, pairs):
        st["writes"].append((row_num, pairs))
        for col, val in pairs:           # reflect the write so a second run sees it
            st["rows"][row_num - 4][26] = val
    monkeypatch.setattr(sheet_writer, "read_sheet", read)
    monkeypatch.setattr(sheet_writer, "safe_write_row", write)
    return st


def test_modern_p_slug_url_gets_trailing_id_as_sku(sheet):
    sheet["rows"] = [_row(url="https://www.costco.com/p/-/mode-quito-6-piece-outdoor-patio/4000423485")]
    assert backfill_skus("svc", "Product Tracker", COL) == 1
    assert sheet["writes"] == [(4, [("AA", "4000423485")])]


def test_backfill_is_idempotent(sheet):
    sheet["rows"] = [_row(url="https://www.costco.com/.product.4201032286.html"),
                     _row(url="https://www.costco.com/.product.1.html", sku="1"),   # already set
                     _row(status="", url="https://www.costco.com/.product.2.html")]  # blank row
    assert backfill_skus("svc", "Product Tracker", COL) == 1
    sheet["reads"] = 0
    assert backfill_skus("svc", "Product Tracker", COL) == 0
    assert len(sheet["writes"]) == 1


def test_row_that_moved_before_the_write_is_skipped(sheet):
    sheet["rows"] = [_row(url="https://www.costco.com/.product.4201032286.html")]
    sheet["after_first_read"] = [_row(url="https://www.costco.com/.product.999.html")]
    assert backfill_skus("svc", "Product Tracker", COL) == 0 and sheet["writes"] == []


def test_dry_run_counts_without_writing(sheet):
    sheet["rows"] = [_row(url="https://www.costco.com/.product.4201032286.html")]
    assert backfill_skus("svc", "Product Tracker", COL, apply=False) == 1 and sheet["writes"] == []


def test_no_costco_id_gets_generated_sku():
    (row_num, sku, source, _), = plan_assignments([_row(url="")], COL)[0]
    assert source == "generated" and sku.startswith("X") and row_num == 4
