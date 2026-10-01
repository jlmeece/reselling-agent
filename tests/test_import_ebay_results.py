"""Tests for tools/import_ebay_results.py — the parse/resolve layer only (no sheet I/O)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.import_ebay_results import parse_results_text, resolve_row  # noqa: E402


HEADER = (
    "Line Number,Action,Status,ErrorCode,ErrorMessage,WarningCode,WarningMessage,"
    "Code,Message,ItemID,ReferenceID,ApplicationData,StartTime,EndTime,"
    "AuctionLengthFee,BoldFee,BorderFee,BuyItNowFee,CategoryFeaturedFee,CurrencyID,"
    "FeaturedFee,FeaturedGalleryFee,FixedPriceDurationFee,GalleryFee,GiftIconFee,"
    "HighlightFee,InsertionFee,InternationalInsertionFee,ListingDesignerFee,"
    "ListingFee,PhotoDisplayFee,PhotoFee,ProPackBundleFee,ReserveFee,SchedulingFee,"
    "SubtitleFee,CustomLabel,PrivateNotes,BasicUpgradePackBundleFee,ValuePackBundleFee,"
    "ProPackPlusBundleFee,SellerInventoryID,CrossBorderTradeNorthAmericaFee,"
    "CrossBorderTradeGBFee,RefundFromSeller,TotalRefundToBuyer,CorrelationID"
)


def _success(line, item_id, label):
    return (f"{line},Add,Success,,,,,,,{item_id},,,2026-10-01T15:53:02.624Z,"
            "2026-11-01T16:53:02.624Z,0.0,0.0,0.0,0.0,0.0,USD,0.0,0.0,0.0,0.0,"
            f"0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,{label},,"
            "0.0,0.0,0.0,,,,,,")


def test_parse_results_returns_labels_and_item_ids():
    text = "\n".join([
        HEADER,
        _success(2, "318943101198", "ROW4"),
        _success(13, "318943101181", "ROW36"),
        ('12,Add,Failure,21920468,"Error - """"Does Not Apply"""" is not a valid value '
         'for Size.|500|Size|Does Not Apply|",,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,ROW34,,,,,,,,,,'),
    ])
    successes, failures = parse_results_text(text)
    assert successes == [("ROW4", "318943101198"), ("ROW36", "318943101181")]
    assert len(failures) == 1
    assert failures[0][0] == "ROW34"
    assert "Does Not Apply" in failures[0][1]


def test_parse_results_keeps_sku_labels():
    # A SKU (not "ROWnn") as CustomLabel is kept — resolution happens against the sheet.
    text = "\n".join([HEADER, _success(2, "318943101198", "1234567")])
    successes, failures = parse_results_text(text)
    assert successes == [("1234567", "318943101198")]
    assert failures == []


def test_resolve_row():
    assert resolve_row("ROW42") == 42
    assert resolve_row("1234567") is None
    assert resolve_row("") is None
    assert resolve_row(None) is None
