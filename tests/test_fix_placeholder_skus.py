"""tools/fix_placeholder_skus.py — plan + photo-move logic (no Sheets / eBay calls)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import fix_placeholder_skus as fps
from tools.photo_compositor import safe_sku

COL = {"title": "C", "costco_url": "R", "sku": "AA"}
P_URL = "https://www.costco.com/p/-/14kt-yellow-gold-bracelet/4201002163"


def _row(title, url, sku):
    r = [""] * 27
    r[2], r[17], r[26] = title, url, sku
    return r


def test_is_placeholder():
    assert fps.is_placeholder("X4fb2cfc373") and fps.is_placeholder("ROW12")
    assert not fps.is_placeholder("4201002163") and not fps.is_placeholder("") and not fps.is_placeholder(" ")


def test_plan_maps_placeholder_to_real_id_and_never_touches_real_ids():
    rows = [_row("Bracelet", P_URL, "X4fb2cfc373"),
            _row("Energy", "https://www.costco.com/x.product.4000099948.html", "4000099948"),
            _row("Old style", "https://www.costco.com/.product.1700000.html", "Xaaaa111111"),
            _row("No sku", P_URL.replace("4201002163", "4201000001"), "")]
    fixes, skipped = fps.plan_fixes(rows, 4, COL)
    assert [(f["row"], f["old"], f["new"]) for f in fixes] == [
        (4, "X4fb2cfc373", "4201002163"), (6, "Xaaaa111111", "1700000")]
    assert skipped == []


def test_plan_skips_unparseable_url_and_duplicates():
    rows = [_row("No URL", "", "X1111111111"),
            _row("Dupe A", P_URL, "X2222222222"),
            _row("Dupe B", P_URL, "X3333333333"),
            _row("Taken", "https://www.costco.com/.product.4000099948.html", "X4444444444"),
            _row("Real", "https://www.costco.com/.product.4000099948.html", "4000099948")]
    fixes, skipped = fps.plan_fixes(rows, 4, COL)
    assert fixes == []
    why = {row: w for row, _, _, w in skipped}
    assert "no parseable" in why[4]
    assert "maps from 2 rows" in why[5] and "maps from 2 rows" in why[6]
    assert "already col AA of row 8" in why[7]


def test_photo_moves_use_safe_sku_and_never_overwrite(tmp_path):
    fix = {"old": "X4fb2cfc373", "new": "4201002163"}
    (tmp_path / f"{safe_sku(fix['old'])}.jpg").write_bytes(b"jpg")
    hosted = {"X4fb2cfc373": {"url": "https://i.ebayimg.com/x", "gallery": ["https://i.ebayimg.com/g"]}}
    moves, conflicts = fps.plan_photo_moves(fix, tmp_path, hosted)
    assert moves == [("jpg", "X4fb2cfc373", "4201002163"), ("hosted", "X4fb2cfc373", "4201002163")]
    assert conflicts == []
    fps.apply_photo_moves(moves, tmp_path, hosted)
    assert (tmp_path / "4201002163.jpg").read_bytes() == b"jpg"
    assert not (tmp_path / "X4fb2cfc373.jpg").exists()
    assert hosted == {"4201002163": {"url": "https://i.ebayimg.com/x",
                                     "gallery": ["https://i.ebayimg.com/g"]}}


def test_photo_conflict_is_reported_not_overwritten(tmp_path):
    fix = {"old": "X4fb2cfc373", "new": "4201002163"}
    (tmp_path / "X4fb2cfc373.jpg").write_bytes(b"old")
    (tmp_path / "4201002163.jpg").write_bytes(b"new")
    hosted = {"X4fb2cfc373": {"url": "a"}, "4201002163": {"url": "b"}}
    moves, conflicts = fps.plan_photo_moves(fix, tmp_path, hosted)
    assert moves == [] and len(conflicts) == 2


def test_no_photo_no_moves(tmp_path):
    assert fps.plan_photo_moves({"old": "X1", "new": "123"}, tmp_path, {}) == ([], [])
