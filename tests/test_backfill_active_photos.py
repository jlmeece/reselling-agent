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


# ── wrong-product guard (2026-10-25: A-Z sort left col AA on other rows) ────────────────

def test_sku_not_matching_row_costco_id_is_skipped():
    job = _job(sku="4201018839", costco_url="https://www.costco.com/.product.100296198.html")
    action, detail, _ = plan_item(job, _live(), BRANDED)
    assert action == "skip" and "isn't this row's Costco id 100296198" in detail


def test_sku_matching_row_costco_id_both_url_shapes_revises():
    for url in ("https://www.costco.com/.product.4000423485.html",
                "https://www.costco.com/p/-/mode-quito-6-piece/4000423485"):
        assert plan_item(_job(sku="4000423485", costco_url=url), _live(), BRANDED)[0] == "revise"


def test_live_title_unlike_row_title_is_skipped():
    job = _job(title="Kirkland Signature Protein Bar Variety Pack")
    action, detail, _ = plan_item(job, _live(title="KitchenAid 1.2 cu ft Countertop Microwave"), BRANDED)
    assert action == "skip" and "doesn't match this row" in detail
    assert plan_item(job, _live(title="Kirkland Protein Bar Variety 42ct"), BRANDED)[0] == "revise"


def test_build_targets_filters_by_item_id_and_carries_costco_url():
    cols = {"status": 0, "title": 2, "sku": 26, "image_urls": 45, "ebay_listing_url": 16, "costco_url": 17}
    r1, r2 = [""] * 46, [""] * 46
    for r, iid, sku in ((r1, "860077013393", "4000423485"), (r2, "860083308423", "4201032286")):
        r[0], r[2], r[26], r[45], r[16] = "ACTIVE", "T", sku, "https://a/i.jpg", f"https://www.ebay.com/itm/{iid}"
        r[17] = f"https://www.costco.com/.product.{sku}.html"
    (job,) = build_targets([r1, r2], 4, cols, item_id="860077013393")
    assert job["sku"] == "4000423485" and job["costco_url"].endswith("4000423485.html")


def test_dry_run_never_builds_uploads_or_revises(monkeypatch, capsys):
    import pytest
    from tools import backfill_active_photos as bap
    r = [""] * 46
    r[0], r[2], r[26], r[45] = "ACTIVE", "Ring", "4000423485", "https://a/i.jpg"
    r[16], r[17] = "https://www.ebay.com/itm/860077013393", "https://www.costco.com/.product.4000423485.html"
    monkeypatch.setattr(bap.pc, "read_rows", lambda: ([r], 4))
    monkeypatch.setattr(bap.up, "load_hosted", lambda: {"4000423485": {"url": BRANDED}})
    monkeypatch.setattr(bap, "fetch_item_price", lambda iid: _live(title="Ring"))   # read-only GetItem
    for name in ("revise_picture",):
        monkeypatch.setattr(bap, name, lambda *a, **k: pytest.fail("dry run must not revise"))
    monkeypatch.setattr(bap.up, "run", lambda **k: pytest.fail("dry run must not upload"))
    monkeypatch.setattr(bap.pc, "run", lambda *a, **k: pytest.fail("dry run must not build photos"))
    monkeypatch.setattr(bap.pc, "load_template", lambda: pytest.fail("dry run must not load the template"))
    assert bap.main(["--dry-run"]) == 0
    assert "would revise" in capsys.readouterr().out
