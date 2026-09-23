"""parse_dimensions — pure text parsing, no browser needed."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.costco_scraper import parse_dimensions


def test_labeled_rows():
    text = "Item Length: 12 in\nItem Width: 5.5 in\nItem Height: 3 in"
    assert parse_dimensions(text) == {"length": 12.0, "width": 5.5, "height": 3.0}


def test_combined_inches():
    assert parse_dimensions("Product Dimensions: 10 x 5 x 3 in") == \
        {"length": 10.0, "width": 5.0, "height": 3.0}


def test_combined_with_axis_letters_and_quotes():
    assert parse_dimensions('Dimensions: 30"L x 20"W x 10"H') == \
        {"length": 30.0, "width": 20.0, "height": 10.0}


def test_centimeters_converted_to_inches():
    d = parse_dimensions("Dimensions: 25.4 x 50.8 x 76.2 cm")
    assert d == {"length": 10.0, "width": 20.0, "height": 30.0}


def test_missing_or_partial_returns_none():
    assert parse_dimensions("") is None
    assert parse_dimensions("no measurements") is None
    assert parse_dimensions("Item Length: 12 in\nItem Width: 5 in") is None
