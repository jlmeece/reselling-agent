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
