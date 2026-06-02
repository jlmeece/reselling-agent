"""
Unit tests for ebay_export.py — no sheet/API calls needed.
Run: python -m pytest tests/test_ebay_export.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import csv
import io
import pytest

from tools.ebay_export import _COL


def _make_row(*, category="Pharmacy", seo_title="Energy Shot 5-Hour 24pk",
              ebay_price="29.99", description="<p>desc</p>", notes="",
              sku="", image_urls="", ebay_url="", title="Energy Shot"):
    """Return a minimal row list matching _COL indices (length 50)."""
    row = [""] * 50
    row[_COL["status"]]      = "READY"
    row[_COL["title"]]       = title
    row[_COL["category"]]    = category
    row[_COL["ebay_price"]]  = ebay_price
    row[_COL["ebay_url"]]    = ebay_url
    row[_COL["sku"]]         = sku
    row[_COL["seo_title"]]   = seo_title
    row[_COL["description"]] = description
    row[_COL["image_urls"]]  = image_urls
    row[_COL["notes"]]       = notes
    return row


def _config_with_id():
    return {
        "business": {},
        "categories": {
            "Pharmacy": {"ebay_category_id": "11897"},
        },
    }


def _config_without_id():
    return {
        "business": {},
        "categories": {
            "Pharmacy": {},          # no ebay_category_id
        },
    }


# ── Category ID: skip when missing ───────────────────────────────────────────

def test_generate_ebay_csv_skips_row_with_no_category_id():
    """A row whose category has no eBay ID should be skipped (not exported)."""
    from tools.ebay_export import generate_ebay_csv
    row = _make_row(category="Pharmacy")
    csv_text = generate_ebay_csv([(4, row)], _config_without_id())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 0, "Row with missing category ID must be skipped"


def test_generate_ebay_csv_exports_row_with_valid_category_id():
    """A row with a mapped eBay category ID should appear in the export."""
    from tools.ebay_export import generate_ebay_csv
    row = _make_row(category="Pharmacy")
    csv_text = generate_ebay_csv([(4, row)], _config_with_id())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 1, "Row with valid category ID must be exported"


def test_generate_ebay_csv_uses_correct_category_id():
    """The Category field in the CSV must match the configured eBay ID."""
    from tools.ebay_export import generate_ebay_csv
    row = _make_row(category="Pharmacy")
    csv_text = generate_ebay_csv([(4, row)], _config_with_id())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows[0]["Category"] == "11897"


# ── Existing skip conditions still work ───────────────────────────────────────

def test_generate_ebay_csv_skips_row_already_listed():
    """Row with an eBay URL already set must be skipped."""
    from tools.ebay_export import generate_ebay_csv
    row = _make_row(ebay_url="https://www.ebay.com/itm/12345")
    csv_text = generate_ebay_csv([(4, row)], _config_with_id())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 0


def test_generate_ebay_csv_skips_row_with_no_seo_title():
    """Row without a seo_title must be skipped."""
    from tools.ebay_export import generate_ebay_csv
    row = _make_row(seo_title="")
    csv_text = generate_ebay_csv([(4, row)], _config_with_id())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 0
