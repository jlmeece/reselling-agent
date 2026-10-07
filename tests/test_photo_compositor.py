"""Tests for tools/photo_compositor.py — synthetic images only, no network or sheet."""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tools import photo_compositor as pc

COLS = {"status": 0, "title": 2, "sku": 26, "image_urls": 45}


def _product(size=(400, 400), touch_corner=False):
    """White photo with a dark box that holds an enclosed white 'label'."""
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    box = (0, 0, 250, 250) if touch_corner else (100, 100, 300, 300)
    d.rectangle(box, fill=(30, 60, 90))
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    d.rectangle((cx - 30, cy - 30, cx + 30, cy + 30), fill=(255, 255, 255))
    return img


def _template(tmp_path, hole=True, background=False):
    tpl = Image.new("RGBA", pc.CANVAS_SIZE, (200, 200, 200, 255))
    if hole:
        ImageDraw.Draw(tpl).rectangle((50, 150, 749, 1049), fill=(0, 0, 0, 0))
    path = tmp_path / "template_blank.png"
    tpl.save(path)
    if background:
        Image.new("RGBA", pc.CANVAS_SIZE, (0, 255, 0, 255)).save(tmp_path / "template_background.png")
    return path


def _row(status="READY", sku="X1", urls="https://a/1.jpg", title="Thing"):
    row = [""] * 46
    row[0], row[2], row[26], row[45] = status, title, sku, urls
    return row


# ── background removal ──────────────────────────────────────────────────────

def test_white_border_becomes_transparent_and_inner_white_survives():
    out = pc.remove_white_background(_product())
    a = out.getchannel("A")
    assert a.getpixel((5, 5)) == 0                 # background
    assert a.getpixel((200, 120)) == 255           # product body
    assert a.getpixel((200, 200)) == 255           # enclosed white label kept


def test_product_touching_corner_still_clears_other_white():
    a = pc.remove_white_background(_product(touch_corner=True)).getchannel("A")
    assert a.getpixel((0, 0)) == 255               # product in the corner
    assert a.getpixel((390, 390)) == 0             # rest of the white is gone
    assert a.getpixel((125, 125)) == 255           # label inside product


def test_large_enclosed_pure_white_is_cleared_but_shaded_white_kept():
    """Bracelet loop: the big pure-white inside goes; an equally big off-white (shaded
    product surface) enclosed region stays."""
    img = Image.new("RGB", (400, 400), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((40, 100, 200, 300), outline=(200, 160, 40), width=8)          # loop, pure-white inside
    d.ellipse((220, 100, 380, 300), fill=(200, 160, 40))
    d.ellipse((235, 115, 365, 285), fill=(244, 244, 244))                    # shaded white, enclosed
    a = pc.remove_white_background(img).getchannel("A")
    assert a.getpixel((120, 200)) == 0             # inside the loop → background
    assert a.getpixel((44, 200)) == 255            # the loop itself
    assert a.getpixel((300, 200)) == 255           # shaded white surface kept


def test_existing_alpha_is_kept():
    img = Image.new("RGBA", (50, 50), (255, 255, 255, 0))
    ImageDraw.Draw(img).rectangle((10, 10, 40, 40), fill=(255, 255, 255, 255))
    out = pc.remove_white_background(img)
    assert out.getchannel("A").getpixel((25, 25)) == 255   # opaque white not flood-filled


def _lifestyle():
    """Photo with non-white edges and a white wall touching the top edge."""
    img = Image.new("RGB", (400, 400), (90, 120, 60))
    ImageDraw.Draw(img).rectangle((100, 0, 200, 150), fill=(255, 255, 255))
    return img


def test_lifestyle_photo_is_not_flooded():
    img = _lifestyle()
    assert not pc.is_white_background(img)
    a = pc.remove_white_background(img).getchannel("A")
    assert a.getextrema() == (255, 255)            # white wall kept, nothing transparent


def test_fetch_prefers_first_white_background_shot():
    imgs = {"u1": _lifestyle(), "u2": _product(), "u3": _product()}
    assert pc.fetch_product_image(["u1", "u2", "u3"], imgs.__getitem__) is imgs["u2"]
    only_life = {"u1": _lifestyle()}
    assert pc.fetch_product_image(["u1"], only_life.__getitem__) is only_life["u1"]


def test_trim_and_fit_center():
    cut = pc.trim(pc.remove_white_background(_product()))
    assert 195 <= cut.width <= 205 and 195 <= cut.height <= 205
    wide = Image.new("RGBA", (400, 100))
    placed, (x, y) = pc.fit_into_box(wide, (60, 190, 700, 900))
    assert placed.size == (700, 175)
    assert (x, y) == (60, 190 + (900 - 175) // 2)


# ── template ────────────────────────────────────────────────────────────────

def test_missing_template_message(tmp_path):
    with pytest.raises(FileNotFoundError) as e:
        pc.load_template(tmp_path / "template_blank.png")
    assert str(e.value) == pc.TEMPLATE_MISSING_MSG


def test_box_from_transparent_hole(tmp_path):
    tpl = pc.load_template(_template(tmp_path))
    assert tpl["hole"] and tpl["box"] == (50, 150, 700, 900)


def test_opaque_template_uses_default_box_and_layout_override(tmp_path):
    path = _template(tmp_path, hole=False)
    assert pc.load_template(path)["box"] == pc.PRODUCT_BOX
    (tmp_path / "layout.json").write_text('{"product_box": [1, 2, 3, 4]}')
    assert pc.load_template(path)["box"] == (1, 2, 3, 4)


def test_compose_layers_background_through_hole(tmp_path):
    tpl = pc.load_template(_template(tmp_path, background=True))
    out = pc.compose(_product(), tpl)
    assert out.size == pc.CANVAS_SIZE and out.mode == "RGB"
    assert out.getpixel((10, 10)) == (200, 200, 200)          # template frame on top
    assert out.getpixel((60, 160)) == (0, 255, 0)             # tile shows where product was white
    assert out.getpixel((400, 600))[:3] != (0, 255, 0)        # product in the middle of the box


# ── sheet rows / urls ───────────────────────────────────────────────────────

def test_parse_image_urls_mixed_separators():
    cell = "https://a/1.avif;x=1 | https://a/2.jpg,data:image/gif;base64,AA, https://a/3.jpg"
    assert pc.parse_image_urls(cell) == ["https://a/1.avif", "https://a/2.jpg", "https://a/3.jpg"]


def test_hi_res_url_only_rewrites_costco_cdn():
    u = "https://gdx-assets.costco.com/adobe/assets/x/as/1-847__1.avif?width=727&height=727&fit=contain"
    assert pc.hi_res_url(u) == u.replace("727", "1500")
    other = "https://content.syndigo.com/asset/abc/thumbnails/240.jpg?width=240"
    assert pc.hi_res_url(other) == other


def test_hi_res_failure_falls_back_to_original_url():
    u = "https://gdx-assets.costco.com/a.avif?width=727&height=727"
    seen = []

    def fetch(url):
        seen.append(url)
        if "1500" in url:
            raise OSError("400")
        return _product()

    pc.fetch_product_image([u], fetch)
    assert seen == [pc.hi_res_url(u), u]


def test_build_jobs_filters_status_and_single_mode():
    rows = [_row("READY", "A"), _row("ACTIVE", "B"), [], _row("APPROVED", "C", urls="")]
    jobs = pc.build_jobs(rows, 4, COLS)
    assert [(j["sku"], j["row"]) for j in jobs] == [("A", 4), ("C", 7)]
    single = pc.build_jobs(rows, 4, COLS, sku="B")
    assert [j["sku"] for j in single] == ["B"]


# ── run ─────────────────────────────────────────────────────────────────────

def test_run_writes_skips_and_counts(tmp_path):
    tpl = pc.load_template(_template(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "DONE.jpg").write_bytes(b"x")
    jobs = pc.build_jobs([_row(sku="NEW"), _row(sku="DONE"), _row(sku="NOIMG", urls=""),
                          _row(sku="")], 4, COLS)
    stats = pc.run(jobs, tpl, output_dir=out_dir, fetch=lambda url: _product())
    assert (stats["processed"], stats["done"], stats["no_image"], stats["no_sku"]) == (1, 1, 1, 1)
    with Image.open(out_dir / "NEW.jpg") as img:
        assert img.size == pc.CANVAS_SIZE and img.format == "JPEG"
    assert "processed 1 / skipped 1 (already done) / skipped 1 (no image URL)" in pc.format_summary(stats)


def test_run_falls_back_to_next_url_and_reports_failures(tmp_path):
    tpl = pc.load_template(_template(tmp_path))
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("bad.jpg"):
            raise OSError("404")
        return _product()

    jobs = pc.build_jobs([_row(sku="OK", urls="https://a/bad.jpg,https://a/good.jpg"),
                          _row(sku="BAD", urls="https://a/bad.jpg")], 4, COLS)
    stats = pc.run(jobs, tpl, output_dir=tmp_path, fetch=fetch)
    assert stats["processed"] == 1 and [s for s, _ in stats["failed"]] == ["BAD"]
    assert (tmp_path / "OK.jpg").exists() and not (tmp_path / "BAD.jpg").exists()


def test_dry_run_and_force(tmp_path):
    (tmp_path / "A.jpg").write_bytes(b"x")
    jobs = pc.build_jobs([_row(sku="A")], 4, COLS)

    def boom(url):
        raise AssertionError("dry run must not download")

    stats = pc.run(jobs, None, dry_run=True, force=True, output_dir=tmp_path, fetch=boom)
    assert stats["processed"] == 1
    assert pc.run(jobs, None, dry_run=True, output_dir=tmp_path, fetch=boom)["done"] == 1


def test_main_missing_template_exits_1(tmp_path, capsys):
    rc = pc.main(["--mode", "sheet", "--template", str(tmp_path / "nope.png")])
    assert rc == 1
    assert pc.TEMPLATE_MISSING_MSG in capsys.readouterr().out
