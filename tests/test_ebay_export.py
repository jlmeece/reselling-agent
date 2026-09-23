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


# ── PicURL: placeholder and separator ─────────────────────────────────────────

def test_generate_ebay_csv_uses_placeholder_when_no_image_urls():
    """PicURL must be the placeholder URL when no image URLs are scraped."""
    from tools.ebay_export import generate_ebay_csv, PLACEHOLDER_IMAGE
    row = _make_row(image_urls="")
    csv_text = generate_ebay_csv([(4, row)], _config_with_id())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows[0]["PicURL"] == PLACEHOLDER_IMAGE


def test_generate_ebay_csv_converts_comma_separated_images_to_pipe():
    """Comma-separated image URLs from the sheet must become pipe-separated in PicURL."""
    from tools.ebay_export import generate_ebay_csv
    urls = "https://example.com/a.jpg,https://example.com/b.jpg"
    row = _make_row(image_urls=urls)
    csv_text = generate_ebay_csv([(4, row)], _config_with_id())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows[0]["PicURL"] == "https://example.com/a.jpg|https://example.com/b.jpg"


# ── Quantity: purchase limit (col W) or DEFAULT_QUANTITY ──────────────────────

@pytest.mark.parametrize("cell,expected", [
    ("4/day", "4"), ("10/day", "10"), (" 2 / day ", "2"), ("3", "3"),
    ("", "99"), ("0/day", "99"), ("n/a", "99"), ("Available (limited)", "99"),
])
def test_quantity_from_limit(cell, expected):
    from tools.ebay_export import _quantity_from_limit
    assert _quantity_from_limit(cell) == expected


def test_default_quantity_constant_is_99():
    from tools.ebay_export import DEFAULT_QUANTITY
    assert DEFAULT_QUANTITY == 99


def test_csv_quantity_uses_purchase_limit_column():
    from tools.ebay_export import generate_ebay_csv
    row = _make_row()
    row[_COL["purchase_limit"]] = "4/day"
    rows = list(csv.DictReader(io.StringIO(generate_ebay_csv([(4, row)], _config_with_id()))))
    assert rows[0]["Quantity"] == "4"


def test_csv_quantity_defaults_and_ignores_notes_regex():
    from tools.ebay_export import generate_ebay_csv
    row = _make_row(notes="Purchase limit: 7/day — list max 7 units on eBay")
    rows = list(csv.DictReader(io.StringIO(generate_ebay_csv([(4, row)], _config_with_id()))))
    assert rows[0]["Quantity"] == "99"


# ── Category migration ────────────────────────────────────────────────────────

def test_yaml_pharmacy_has_no_stale_category_id():
    from tools.ebay_export import _load_config
    pharm = _load_config()["categories"]["Pharmacy"]
    ids = {str(pharm["ebay_category_id"])} | {str(v) for v in pharm["ebay_category_map"].values()}
    assert "11896" not in ids
    assert str(pharm["ebay_category_map"]["default"]) == "183904"


def test_stale_category_id_is_migrated_on_export():
    from tools.ebay_export import generate_ebay_csv
    config = {"business": {}, "categories": {"Pharmacy": {"ebay_category_id": "11896"}}}
    rows = list(csv.DictReader(io.StringIO(generate_ebay_csv([(4, _make_row())], config))))
    assert rows[0]["Category"] == "183904"


# ── Item specifics: required ones are never blank ─────────────────────────────

def _export_one(category, title, notes="", cfg=None):
    from tools.ebay_export import generate_ebay_csv, _load_config
    config = cfg or _load_config()
    row = _make_row(category=category, title=title, seo_title=title, notes=notes)
    return list(csv.DictReader(io.StringIO(generate_ebay_csv([(4, row)], config))))[0]


def test_pharmacy_gets_color_model_and_dimensions():
    r = _export_one("Pharmacy", "Kirkland Signature Fish Oil 1000 mg, 400 Softgels")
    assert r["C:Color"] == "Does Not Apply"
    assert r["C:Model"] == "Does Not Apply"
    assert r["C:Item Length"] == "4 in" and r["C:Item Width"] == "4 in" and r["C:Item Height"] == "6 in"


def test_scraped_dimensions_and_model_from_notes_win():
    notes = "Costco specs: Brand: Vitamix | Model: A3500 | Dimensions: 11.5 x 8 x 17.5 in"
    r = _export_one("Small Appliances", "Vitamix Ascent Blender Black", notes)
    assert (r["C:Item Length"], r["C:Item Width"], r["C:Item Height"]) == ("11.5 in", "8 in", "17.5 in")
    assert r["C:Model"] == "A3500"
    assert r["C:Color"] == "Black"
    assert r["C:Brand"] == "Vitamix"


def test_brand_builder_lens_text_is_not_a_brand():
    r = _export_one("Outdoor Furniture", "POLYWOOD Long Beach Adirondack Chair",
                    "Strongest lens: Brand Builder | demand")
    assert r["C:Brand"] == "POLYWOOD"


def test_unknown_brand_falls_back_to_unbranded():
    r = _export_one("Pharmacy", "Some Supplement 100 Tablets")
    assert r["C:Brand"] == "Unbranded"


def test_every_required_specific_is_non_blank_for_every_category():
    from tools.ebay_export import _load_config, _infer_item_specifics
    config = _load_config()
    for category, cat_config in config["categories"].items():
        required = cat_config.get("ebay_required_specifics")
        if not required:
            continue
        specifics = _infer_item_specifics("Generic Product", category, "", cat_config, "")
        blanks = [k for k in required if not specifics.get(k)]
        assert not blanks, f"{category}: blank required specifics {blanks}"


def test_required_specifics_are_all_csv_columns():
    from tools.ebay_export import _load_config, _EBAY_COLUMNS
    for category, cat_config in _load_config()["categories"].items():
        for key in cat_config.get("ebay_required_specifics", []):
            assert key in _EBAY_COLUMNS, f"{category}: {key} has no CSV column"


def test_parse_dimensions_from_notes():
    from tools.ebay_export import _parse_dimensions_from_notes
    assert _parse_dimensions_from_notes("x\nCostco specs: Dimensions: 10 x 5.5 x 3 in") == \
        {"length": 10.0, "width": 5.5, "height": 3.0}
    assert _parse_dimensions_from_notes("no dims here") is None
