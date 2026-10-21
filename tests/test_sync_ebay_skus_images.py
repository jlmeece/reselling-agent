"""tools/sync_ebay_skus.py + tools/backfill_images.py — plan logic (no eBay / Sheets / Chrome)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backfill_images import classify, find_targets
from tools.sync_ebay_skus import plan

COL = {"status": "A", "title": "C", "ebay_listing_url": "Q", "costco_url": "R", "sku": "AA",
       "image_urls": "AT"}


def _row(status="ACTIVE", title="Chair", q="", sku="", url="https://www.costco.com/.product.1.html", at=""):
    r = [""] * 46
    r[0], r[2], r[16], r[17], r[26], r[45] = status, title, q, url, sku, at
    return r


def _l(iid, sku="ROW4", ltype="FixedPriceItem"):
    return {"item_id": iid, "title": "Chair", "sku": sku, "listing_type": ltype}


def test_sku_plan_changes_matches_and_skips():
    rows = [_row(q="https://www.ebay.com/itm/111111111111", sku="100404264"),
            _row(q="222222222222", sku="X1"),
            _row(q="333333333333", sku=""),
            _row(q="444444444444", sku="A"), _row(q="444444444444", sku="B"),
            _row(status="WATCH", q="555555555555", sku="777")]
    listings = [_l("111111111111"), _l("222222222222", sku="X1"), _l("333333333333"),
                _l("444444444444"), _l("555555555555"), _l("999999999999"),
                _l("666666666666", ltype="Chinese")]
    rows.append(_row(q="666666666666", sku="Z"))
    changes, unchanged, skipped = plan(listings, rows, 4, COL)
    assert [(l["item_id"], row, sku) for l, row, sku in changes] == [
        ("111111111111", 4, "100404264"), ("555555555555", 9, "777")]       # any status, by col Q
    assert [l["item_id"] for l in unchanged] == ["222222222222"]
    why = {l["item_id"]: w for l, w in skipped}
    assert why["333333333333"].startswith("blank col AA")
    assert "2 sheet rows" in why["444444444444"]
    assert why["999999999999"] == "no sheet row with this item id in col Q"
    assert why["666666666666"] == "listing type Chinese"


def test_image_targets_are_active_live_rows_with_empty_at():
    rows = [_row(q="111111111111"), _row(q="222222222222", at="https://a/x.jpg"),
            _row(status="APPROVED", q="333333333333"), _row(q="")]
    assert [(t["row"], t["item_id"]) for t in find_targets(rows, 4, COL)] == [(4, "111111111111")]


def test_classify_scrape_results():
    imgs = [f"https://gdx-assets.costco.com/{i}.avif" for i in range(8)]
    assert classify({"image_urls": imgs, "in_stock": True})[0] == imgs[:5]
    assert classify({"removed": True})[1].startswith("Costco page removed")
    assert classify({"stock_status": "CHECK FAILED"})[1].startswith("scrape failed")
    assert classify({"in_stock": False, "stock_status": "OUT OF STOCK", "image_urls": imgs})[1] == \
        "out of stock at Costco (OUT OF STOCK)"
    assert classify({"in_stock": True, "image_urls": []})[1] == "no images on the Costco page"


def test_with_live_sku_reads_getitem():
    from tools.sync_ebay_skus import with_live_sku
    live = {"111111111111": {"ok": True, "sku": "ROW4", "listing_type": "FixedPriceItem"},
            "222222222222": {"ok": False}}
    out = with_live_sku([_l("111111111111", sku=""), _l("222222222222", sku="")], fetch=lambda i: live[i])
    assert [(l["sku"], l["listing_type"]) for l in out] == [("ROW4", "FixedPriceItem"), (None, "FixedPriceItem")]
    rows = [_row(q="111111111111", sku="ROW4")]
    assert plan(out[:1], rows, 4, COL)[1] and not plan(out[:1], rows, 4, COL)[0]   # already matching
