"""tools/backfill_active_photos.py — plan logic only (no eBay / Sheets / image calls)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backfill_active_photos import build_targets, plan_item

BRANDED = "https://i.ebayimg.com/00/s/MTI4MFgxMjAw/z/BRAND1/$_1.JPG?set_id=2"
OLD = ["https://i.ebayimg.com/00/s/NzI3/z/OLD1/$_1.JPG", "https://i.ebayimg.com/00/s/NzI3/z/OLD2/$_1.JPG"]


def _job(**kw):
    return {"row": 6, "status": "ACTIVE", "title": "Ring", "sku": "X1", "item_id": "318943101198",
            "urls": ["https://gdx-assets.costco.com/a.avif"], **kw}


def _live(**kw):
    return {"ok": True, "listing_status": "Active", "listing_type": "FixedPriceItem",
            "has_variations": False, "picture_urls": list(OLD), "error_kind": None,
            "error_code": "", "message": "", **kw}


def test_revise_keeps_existing_gallery_after_new_main():
    action, _, gallery = plan_item(_job(), _live(), BRANDED)
    assert action == "revise" and gallery == OLD


def test_already_branded_is_skipped():
    live = _live(picture_urls=[BRANDED.replace("set_id=2", "set_id=8")] + OLD)
    assert plan_item(_job(), live, BRANDED)[:2] == ("skip", "already has the branded main photo")


def test_branded_elsewhere_in_gallery_is_not_duplicated():
    _, _, gallery = plan_item(_job(), _live(picture_urls=OLD + [BRANDED]), BRANDED)
    assert gallery == OLD


def test_gallery_capped_to_leave_room_for_main():
    pics = [f"https://i.ebayimg.com/00/s/x/z/P{i}/$_1.JPG" for i in range(30)]
    assert len(plan_item(_job(), _live(picture_urls=pics), BRANDED)[2]) == 23


def test_skip_reasons():
    assert plan_item(_job(sku=""), _live(), None)[1] == "no SKU in col AA"
    assert plan_item(_job(urls=[]), _live(), None)[1] == "no image URL in col AT"
    assert plan_item(_job(item_id=None), _live(), None)[1] == "no eBay item id in col Q"
    assert "multi-variation" in plan_item(_job(), _live(has_variations=True), None)[1]
    assert "not FixedPriceItem" in plan_item(_job(), _live(listing_type="Chinese"), None)[1]
    assert "Ended" in plan_item(_job(), _live(listing_status="Ended"), None)[1]
    failed = _live(ok=False, error_code="21920397", message="policy")
    assert "GetItem failed (21920397" in plan_item(_job(), failed, None)[1]


def test_build_targets_every_row_with_item_id_any_status():
    cols = {"status": 0, "title": 2, "sku": 26, "image_urls": 45, "ebay_listing_url": 16}

    def row(status, sku, q):
        r = [""] * 46
        r[0], r[2], r[26], r[45], r[16] = status, "T", sku, "https://a/img.jpg", q
        return r
    rows = [row("ACTIVE", "A", "https://www.ebay.com/itm/318943101198"), row("READY", "B", ""),
            row("ACTIVE", "C", ""),
            row("PAUSED_MARGIN", "D", "https://www.ebay.com/itm/318925902426"),   # live, non-ACTIVE
            row("AUDIT_REVIEW", "E", "860016681733"),                             # bare id in col Q
            row("PAUSED_OOS", "F", "https://www.ebay.com/itm/318910010687"),      # hidden: GetItem decides
            row("WATCH", "G", "")]                                                # no col Q -> skipped
    out = build_targets(rows, 4, cols)
    assert [(j["sku"], j["status"], j["item_id"]) for j in out] == [
        ("A", "ACTIVE", "318943101198"), ("D", "PAUSED_MARGIN", "318925902426"),
        ("E", "AUDIT_REVIEW", "860016681733"), ("F", "PAUSED_OOS", "318910010687")]
    assert out[1]["urls"] == ["https://a/img.jpg"] and out[1]["row"] == 7
    assert [j["sku"] for j in build_targets(rows, 4, cols, sku="D")] == ["D"]
    assert build_targets(rows, 4, cols, sku="C") == []               # no col Q -> never a target


def test_non_active_listing_not_active_on_ebay_is_skipped():
    action, detail, _ = plan_item(_job(status="PAUSED_MARGIN"), _live(listing_status="Completed"), None)
    assert action == "skip" and "Completed" in detail
