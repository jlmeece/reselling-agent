"""
photo_compositor.py — Branded eBay main listing photos from Costco product images.

For each APPROVED/READY row with images in col AT, downloads the first usable image,
turns its white background transparent (flood-fill from the edges, so white parts of
the product survive), fits it into the template's product zone and saves
data/listing_photos/<sku>.jpg (1200x1280, JPEG q92). Read-only on the sheet.

Template files (data/photo_template/):
  template_blank.png       REQUIRED. Jay's Photoshop export. If it has a transparent
                           hole, the product box is that hole and the template is laid
                           OVER the product (badges/watermark stay on top). If it is
                           fully opaque, the product is pasted on top of it inside
                           PRODUCT_BOX (or layout.json's "product_box").
  template_background.png  optional tile-only layer shown through the hole (else white)
  template_overlay.png     optional top layer (e.g. a watermark-only export)
  layout.json              optional {"product_box": [x, y, w, h]} override

Usage:
  python tools/photo_compositor.py --mode sheet [--force] [--limit N]
  python tools/photo_compositor.py --mode single --sku X0bac7f5423
  python tools/photo_compositor.py --dry-run
"""

import argparse
import json
import os
import re
import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv
from loguru import logger
from PIL import Image, ImageChops, ImageDraw, ImageFilter

load_dotenv(encoding="utf-8", override=True)

ROOT          = Path(__file__).resolve().parent.parent
TEMPLATE_DIR  = ROOT / "data" / "photo_template"
TEMPLATE_PATH = TEMPLATE_DIR / "template_blank.png"
OUTPUT_DIR    = ROOT / "data" / "listing_photos"

CANVAS_SIZE   = (1200, 1280)
# (x, y, w, h) — left/center zone, used only when the template has no transparent hole.
PRODUCT_BOX   = (60, 190, 700, 900)
JPEG_QUALITY  = 92
WHITE_THRESHOLD = 240      # R, G and B all above this = background white
MAX_URL_TRIES = 3          # fall back to the next col AT image if the first is unusable
MIN_SOURCE_PX = 500        # warn below this (upscaled → soft)
HI_RES_PX     = 1500       # size requested from Costco's resizing CDN (verified to serve it)
MIN_WHITE_BORDER = 0.6     # share of edge pixels near-white for a "studio shot" (bg gets removed)
# Enclosed white (inside a bracelet loop) is background too when it's this pure and at
# least this share of the image. 0 disables (strict "only edge-connected white" rule).
ENCLOSED_WHITE_MIN = 250
ENCLOSED_MIN_AREA  = 0.05
ELIGIBLE_STATUSES = {"APPROVED", "READY"}

TEMPLATE_MISSING_MSG = (
    "Template not found at data/photo_template/template_blank.png — export your blank "
    "template from Photoshop as a PNG with the product area transparent, then re-run."
)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "image/avif,image/webp,image/jpeg,image/png,image/*;q=0.8",
}


# ── Sheet ─────────────────────────────────────────────────────────────────────

def _col_idx(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _load_columns() -> dict:
    with open(ROOT / "config" / "col_map.yaml") as f:
        cols = yaml.safe_load(f)["columns"]
    return {k: _col_idx(cols[k]) for k in ("status", "title", "sku", "image_urls")}


def _cell(row: list, i: int) -> str:
    return str(row[i]).strip() if i < len(row) else ""


def parse_image_urls(cell: str) -> list[str]:
    """Col AT is ','- or ' | '-separated depending on the writer; keep fetchable URLs."""
    from tools.ebay_export import _sanitize_pic_url
    urls = [_sanitize_pic_url(p) for p in re.split(r"[,|]", cell or "")]
    return [u for u in urls if u]


def safe_sku(sku: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", sku.strip())


def build_jobs(rows: list[list], start_row: int, cols: dict, sku: str | None = None) -> list[dict]:
    """Rows → jobs. sheet mode: APPROVED/READY only. single mode (sku given): any status."""
    jobs = []
    for offset, row in enumerate(rows):
        if not row:
            continue
        status = _cell(row, cols["status"]).upper()
        row_sku = _cell(row, cols["sku"])
        if sku is not None:
            if row_sku != sku:
                continue
            if status not in ELIGIBLE_STATUSES:
                logger.warning(f"SKU {sku} is {status or 'blank'}, not APPROVED/READY — processing anyway")
        elif status not in ELIGIBLE_STATUSES:
            continue
        jobs.append({
            "row":    start_row + offset,
            "status": status,
            "title":  _cell(row, cols["title"]),
            "sku":    row_sku,
            "urls":   parse_image_urls(_cell(row, cols["image_urls"])),
        })
    return jobs


def read_rows() -> tuple[list[list], int]:
    from tools.sheet_writer import get_sheets_service, read_sheet
    with open(ROOT / "config" / "categories.yaml") as f:
        business = yaml.safe_load(f)["business"]
    start, end = business["data_start_row"], business["data_end_row"]
    rows = read_sheet(get_sheets_service(), f"'{business['sheet_name']}'!A{start}:AT{end}")
    return rows, start


# ── Template ──────────────────────────────────────────────────────────────────

def _open_layer(path: Path) -> Image.Image | None:
    if not path.exists():
        return None
    img = Image.open(path).convert("RGBA")
    if img.size != CANVAS_SIZE:
        logger.warning(f"{path.name} is {img.size[0]}x{img.size[1]}, expected "
                       f"{CANVAS_SIZE[0]}x{CANVAS_SIZE[1]} — resizing")
        img = img.resize(CANVAS_SIZE, Image.LANCZOS)
    return img


def load_template(template_path: Path = TEMPLATE_PATH) -> dict:
    """Returns {template, background, overlay, box, hole}. Raises FileNotFoundError
    with TEMPLATE_MISSING_MSG when template_blank.png is missing."""
    template = _open_layer(template_path)
    if template is None:
        raise FileNotFoundError(TEMPLATE_MISSING_MSG)
    folder = template_path.parent

    # The transparent hole (alpha < 128) is the product zone.
    hole_mask = template.getchannel("A").point(lambda a: 255 if a < 128 else 0)
    hole_bbox = hole_mask.getbbox()
    if hole_bbox:
        x0, y0, x1, y1 = hole_bbox
        box = (x0, y0, x1 - x0, y1 - y0)
    else:
        box = PRODUCT_BOX
    layout_path = folder / "layout.json"
    if layout_path.exists():
        override = json.loads(layout_path.read_text()).get("product_box")
        if override:
            box = tuple(int(v) for v in override)

    background = _open_layer(folder / "template_background.png")
    if hole_bbox and background is None:
        logger.warning("Template has a transparent product area but no template_background.png — "
                       "the product will sit on plain white. Export the tile layer as "
                       "data/photo_template/template_background.png to show the tiles behind it.")
    return {
        "template":   template,
        "background": background,
        "overlay":    _open_layer(folder / "template_overlay.png"),
        "box":        box,
        "hole":       bool(hole_bbox),
    }


# ── Image processing ─────────────────────────────────────────────────────────

def _has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        return img.convert("RGBA").getchannel("A").getextrema()[0] < 255
    return False


def _white_mask(img: Image.Image, threshold: int = WHITE_THRESHOLD) -> Image.Image:
    r, g, b = (ch.point(lambda v: 255 if v > threshold else 0) for ch in img.convert("RGB").split())
    return ImageChops.multiply(ImageChops.multiply(r, g), b)   # 255 = near-white


def _border(size: tuple[int, int]) -> list[tuple[int, int]]:
    w, h = size
    return ([(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
            + [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)])


def white_border_ratio(img: Image.Image, threshold: int = WHITE_THRESHOLD) -> float:
    """Share of edge pixels that are near-white: ~1.0 for a studio shot, low for a lifestyle photo."""
    if _has_alpha(img):
        return 1.0
    px = _white_mask(img, threshold).load()
    border = _border(img.size)
    return sum(1 for xy in border if px[xy] == 255) / len(border)


def is_white_background(img: Image.Image) -> bool:
    return white_border_ratio(img) >= MIN_WHITE_BORDER


def _enclosed_studio_white(img: Image.Image, background: Image.Image) -> Image.Image:
    """Mask of large, PURE-white regions the edge flood couldn't reach — the inside of a
    bracelet / necklace / ring loop. Studio white is a flat ~255; white product surfaces
    (bottles, caps, labels) have shading below ENCLOSED_WHITE_MIN or are small, so they stay.
    Components are labelled on a downscaled copy; the result is ANDed with the full-res
    pure-white mask so edges stay exact."""
    if ENCLOSED_MIN_AREA <= 0:
        return Image.new("L", img.size, 0)
    pure = ImageChops.subtract(_white_mask(img, ENCLOSED_WHITE_MIN), background)  # 255 = pure white, not yet bg
    scale = min(1.0, 300 / max(img.size))
    small = pure.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                        Image.NEAREST)
    min_px = ENCLOSED_MIN_AREA * small.width * small.height
    px = small.load()
    found = False
    for y in range(small.height):
        for x in range(small.width):
            if px[x, y] != 255:
                continue
            ImageDraw.floodfill(small, (x, y), 64)
            big = small.histogram()[64] >= min_px
            small = small.point(lambda v, big=big: (128 if big else 32) if v == 64 else v)
            px = small.load()
            found = found or big
    if not found:
        return Image.new("L", img.size, 0)
    regions = small.point(lambda v: 255 if v == 128 else 0).resize(img.size, Image.NEAREST)
    regions = regions.filter(ImageFilter.MaxFilter(5))      # cover the downscale's jagged rim
    return ImageChops.multiply(regions, pure)


def remove_white_background(img: Image.Image, threshold: int = WHITE_THRESHOLD) -> Image.Image:
    """Near-white pixels connected to the image edge → transparent. White INSIDE the
    product (labels, caps) is enclosed by product pixels, so the fill never reaches it.
    Images that already have real transparency are returned as-is; a lifestyle photo
    (mostly non-white edges) is kept whole — flooding it would punch holes in white walls/sky."""
    rgba = img.convert("RGBA")
    if _has_alpha(img) or not is_white_background(img):
        return rgba
    white = _white_mask(img, threshold)

    # Flood every near-white border pixel (not just corners — products touch corners).
    px = white.load()
    for xy in _border(white.size):
        if px[xy] == 255:
            ImageDraw.floodfill(white, xy, 128)
    background = white.point(lambda v: 255 if v == 128 else 0)
    background = ImageChops.lighter(background, _enclosed_studio_white(img, background))

    # Grow the background 1px and soften the edge to kill the white halo.
    background = background.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    alpha = ImageChops.invert(background)
    rgba.putalpha(alpha)
    return rgba


def trim(img: Image.Image) -> Image.Image:
    """Crop to the visible (non-transparent) content."""
    bbox = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def fit_into_box(img: Image.Image, box: tuple) -> tuple[Image.Image, tuple[int, int]]:
    """Scale to fit inside box (x, y, w, h) keeping aspect; return image + centered offset."""
    bx, by, bw, bh = box
    scale = min(bw / img.width, bh / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    resized = img.resize(size, Image.LANCZOS)
    return resized, (bx + (bw - size[0]) // 2, by + (bh - size[1]) // 2)


def compose(product: Image.Image, tpl: dict) -> Image.Image:
    """Layer the product with the template. Returns an RGB image at CANVAS_SIZE."""
    cut = trim(remove_white_background(product))
    if max(cut.size) < MIN_SOURCE_PX:
        logger.warning(f"  source product is only {cut.width}x{cut.height}px — upscaled, may look soft")
    placed, offset = fit_into_box(cut, tpl["box"])
    layer = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    layer.paste(placed, offset, placed)

    if tpl["hole"]:
        base = (tpl["background"].copy() if tpl["background"] is not None
                else Image.new("RGBA", CANVAS_SIZE, (255, 255, 255, 255)))
        base.alpha_composite(layer)
        base.alpha_composite(tpl["template"])
    else:
        base = tpl["template"].copy()
        base.alpha_composite(layer)
    if tpl["overlay"] is not None:
        base.alpha_composite(tpl["overlay"])
    flat = Image.new("RGB", CANVAS_SIZE, (255, 255, 255))
    flat.paste(base, (0, 0), base)
    return flat


def save_jpeg(img: Image.Image, path: Path) -> None:
    """Atomic write — a crash never leaves a half-written file that sheet mode would skip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jpg.tmp")
    img.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True, subsampling=0)
    os.replace(tmp, path)


def download_image(url: str, timeout: int = 20) -> Image.Image:
    import requests
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content))   # format sniffed from bytes (.avif-named JPEGs ok)
    img.load()
    return img


def hi_res_url(url: str, px: int = HI_RES_PX) -> str:
    """Costco's AEM CDN resizes on request: col AT holds ?width=727&height=727 (the page's
    gallery size) — ask for px instead so the 700x900 box isn't filled by an upscale."""
    if "gdx-assets.costco.com" not in url:
        return url
    return re.sub(r"([?&](?:width|height)=)\d+", rf"\g<1>{px}", url)


def _fetch_best(url: str, fetch) -> Image.Image:
    big = hi_res_url(url)
    if big != url:
        try:
            return fetch(big)
        except Exception as e:
            logger.debug(f"  hi-res fetch failed ({e}) — using {url[-40:]}")
    return fetch(url)


def fetch_product_image(urls: list[str], fetch=download_image) -> Image.Image:
    """First white-background (studio) shot among the first MAX_URL_TRIES images — Costco
    often leads with a lifestyle photo (furniture). None white → the first that loaded."""
    errors, fallback = [], None
    for i, url in enumerate(urls[:MAX_URL_TRIES]):
        try:
            img = _fetch_best(url, fetch)
        except Exception as e:   # network / HTTP / not an image — try the next one
            errors.append(f"#{i + 1}: {type(e).__name__}: {e}")
            continue
        if is_white_background(img):
            if i:
                logger.info(f"  using image #{i + 1} (first white-background shot)")
            return img
        fallback = fallback or img
    if fallback is not None:
        logger.info("  no white-background shot — using the photo as-is (no bg removal)")
        return fallback
    raise RuntimeError("no usable image — " + "; ".join(errors))


# ── Run ───────────────────────────────────────────────────────────────────────

def run(jobs: list[dict], tpl: dict | None, *, dry_run: bool = False, force: bool = False,
        limit: int | None = None, output_dir: Path = OUTPUT_DIR, fetch=download_image) -> dict:
    stats = {"processed": 0, "done": 0, "no_image": 0, "no_sku": 0, "failed": []}
    for job in jobs:
        label = f"row {job['row']} {job['title'][:45]!r}"
        if not job["sku"]:
            stats["no_sku"] += 1
            logger.info(f"skip (no SKU in col AA): {label}")
            continue
        if not job["urls"]:
            stats["no_image"] += 1
            logger.info(f"skip (no image URL): {job['sku']} {label}")
            continue
        out = output_dir / f"{safe_sku(job['sku'])}.jpg"
        if out.exists() and not force:
            stats["done"] += 1
            continue
        if limit is not None and stats["processed"] >= limit:
            break
        if dry_run:
            print(f"  would process {job['sku']:<14} {label}  ← {job['urls'][0][:80]}")
            stats["processed"] += 1
            continue
        try:
            img = fetch_product_image(job["urls"], fetch)
            save_jpeg(compose(img, tpl), out)
            stats["processed"] += 1
            logger.info(f"✓ {out.name}  ({label})")
        except Exception as e:
            stats["failed"].append((job["sku"], str(e)))
            logger.error(f"✗ {job['sku']} {label}: {e}")
    return stats


def format_summary(stats: dict, dry_run: bool = False) -> str:
    verb = "would process" if dry_run else "processed"
    line = (f"{verb} {stats['processed']} / skipped {stats['done']} (already done) / "
            f"skipped {stats['no_image']} (no image URL) / skipped {stats['no_sku']} (no SKU) / "
            f"failed {len(stats['failed'])}")
    for sku, err in stats["failed"]:
        line += f"\n  FAILED {sku}: {err}"
    return line


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Composite Costco product images onto the branded template.")
    ap.add_argument("--mode", choices=["sheet", "single"], default="sheet")
    ap.add_argument("--sku", help="SKU (col AA) for --mode single")
    ap.add_argument("--dry-run", action="store_true", help="list what would be processed; download nothing")
    ap.add_argument("--force", action="store_true", help="re-render photos that already exist")
    ap.add_argument("--limit", type=int, help="process at most N photos")
    ap.add_argument("--template", type=Path, default=TEMPLATE_PATH, help=argparse.SUPPRESS)
    ap.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):   # Windows console is cp1252 — titles/arrows crash it
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.mode == "single" and not args.sku:
        ap.error("--mode single needs --sku")

    tpl = None
    try:
        tpl = load_template(args.template)
        logger.info(f"Template: product box {tpl['box']} "
                    f"({'transparent hole, template over product' if tpl['hole'] else 'opaque, product on top'})")
    except FileNotFoundError as e:
        print(str(e))
        if not args.dry_run:
            return 1

    rows, start = read_rows()
    jobs = build_jobs(rows, start, _load_columns(), sku=args.sku if args.mode == "single" else None)
    if args.mode == "single" and not jobs:
        print(f"SKU {args.sku} not found in col AA")
        return 1

    stats = run(jobs, tpl, dry_run=args.dry_run, force=args.force or args.mode == "single",
                limit=args.limit, output_dir=args.output_dir)
    print(format_summary(stats, args.dry_run))
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
