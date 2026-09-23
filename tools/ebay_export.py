"""
Tool: ebay_export
Generates an eBay Seller Hub–compatible CSV from approved/watched products in the sheet.

Products must have:
  - Status = APPROVED or WATCH
  - seo_title (col AI) filled in — indicates copy has been generated

Upload the output CSV to eBay Seller Hub → Reports → Uploads → Upload your file.
After upload, add product photos via eBay's bulk image uploader or individual listing editor.

USAGE:
  python tools/ebay_export.py

OUTPUT:
  data/exports/ebay_upload_YYYYMMDD_HHMMSS.csv
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import yaml
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from loguru import logger

load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import get_sheets_service, read_sheet

# Listing quantity when the product has no Costco purchase limit (col W).
DEFAULT_QUANTITY = 99

# eBay retired some category IDs and auto-migrates them; never emit the old ones.
_CATEGORY_MIGRATIONS = {"11896": "183904"}   # Pharmacy: Other Vitamins & Supplements

# Value for a required item specific when there is no real one — eBay rejects blanks.
NOT_APPLICABLE = "Does Not Apply"


# ── Column indices (0-based, matching A=0) ───────────────────────────────────
# Source of truth: config/col_map.yaml — update there first, then here.
# Current visible range A–Z (indices 0–25), hidden AA–AV (indices 26–47).
_COL = {
    "status":       0,   # A
    "demand_score": 1,   # B
    "title":        2,   # C
    "category":     3,   # D
    "ebay_price":   7,   # H
    "notes":        47,  # AV — full research narrative; "Costco specs:" line holds brand/model/dimensions
    "purchase_limit": 22,  # W  — "N/day" (blank when Costco has no limit)
    "sku":          26,  # AA
    "costco_url":   17,  # R
    "ebay_url":     16,  # Q
    "image_urls":   45,  # AT
    "seo_title":    34,  # AI
    "bullets":      35,  # AJ
    "description":  36,  # AK
    "redirect_msg": 37,  # AL
    "keywords":     39,  # AN
}

_EXPORT_STATUSES = {"READY"}
PLACEHOLDER_IMAGE = "https://placehold.co/1600x1600/ffffff/cccccc/png"

# eBay Seller Hub CSV column order
_EBAY_COLUMNS = [
    "Action",
    "SiteID",
    "Country",
    "Category",
    "Title",
    "Subtitle",
    "Format",
    "StartPrice",
    "Quantity",
    "Duration",
    "Description",
    "ConditionID",
    "Location",
    "PicURL",
    "C:Brand",
    "C:Type",
    "C:Material",
    "C:Metal",
    "C:Purity",
    "C:Style",
    "C:Formulation",
    "C:Unit Count",
    "C:Movement",
    "C:Water Resistance",
    "C:Case Color",
    "C:Band Color",
    "C:Model",
    "C:Color",
    "C:Item Width",
    "C:Item Height",
    "C:Item Length",
    "ShippingProfileName",
    "ReturnProfileName",
    "PaymentProfileName",
    "CustomLabel",
]


def _safe(lst: list, i: int, default: str = "") -> str:
    return str(lst[i]).strip() if i < len(lst) else default


def _parse_brand_from_notes(notes: str) -> str:
    """Extract brand from the "Costco specs:" / "Brand:" label in the notes field.

    Requires an explicit colon so the research narrative's "Brand Builder" lens
    text is not mistaken for a brand name.
    """
    m = re.search(r"\bbrand\s*:\s*([^\n|]+)", notes, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _parse_model_from_notes(notes: str) -> str:
    m = re.search(r"\bmodel\s*:\s*([^\n|]+)", notes, re.IGNORECASE)
    return m.group(1).strip() if m else ""


_DIM_NOTE_RX = re.compile(
    r"\bdimensions\s*:\s*([\d.]+)\s*x\s*([\d.]+)\s*x\s*([\d.]+)\s*in\b", re.IGNORECASE
)


def _parse_dimensions_from_notes(notes: str) -> dict | None:
    """Parse "Dimensions: L x W x H in" (written by researcher) → {length,width,height}."""
    m = _DIM_NOTE_RX.search(notes)
    if not m:
        return None
    length, width, height = (float(g) for g in m.groups())
    if min(length, width, height) <= 0:
        return None
    return {"length": length, "width": width, "height": height}


_TITLE_BRAND_MAP = [
    ("kirkland signature", "Kirkland Signature"),
    ("kirkland",           "Kirkland Signature"),
    ("polywood",           "POLYWOOD"),
    ("henredon",           "Henredon"),
    ("cuisinart",          "Cuisinart"),
    ("vitamix",            "Vitamix"),
    ("kitchenaid",         "KitchenAid"),
    ("nature made",        "Nature Made"),
    ("ninja",              "Ninja"),
    ("instant pot",        "Instant Pot"),
    ("pamp suisse",        "PAMP Suisse"),
]


def _brand_from_title(title: str) -> str:
    """Fallback: infer brand from product title when notes parsing returns empty."""
    t = title.lower()
    for prefix, brand in _TITLE_BRAND_MAP:
        if prefix in t:
            return brand
    return ""


_CAT_CACHE_PATH = Path(__file__).parent.parent / "data" / "ebay_category_cache.json"


def _load_cat_cache() -> dict:
    if _CAT_CACHE_PATH.exists():
        try:
            return json.loads(_CAT_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cat_cache(cache: dict) -> None:
    try:
        _CAT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CAT_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _serper_category_lookup(title: str) -> str:
    """
    Uses Serper to find a real eBay browse URL for this product title,
    then extracts the category ID from the URL pattern /b/Name/CATID/...
    Results cached in data/ebay_category_cache.json so each title only hits the API once.
    Returns category ID string, or "" on failure (falls back to categories.yaml).
    """
    serper_key = os.getenv("SERPER_API_KEY", "")
    if not serper_key:
        return ""

    cache = _load_cat_cache()
    cache_key = title.lower().strip()
    if cache_key in cache:
        cached = cache[cache_key]
        if cached:
            logger.debug(f"  Category cache hit: {title[:40]} → {cached}")
        return cached

    try:
        query = f"{title} site:ebay.com/b/"
        payload = json.dumps({"q": query, "num": 5}).encode()
        req = urllib.request.Request(
            "https://google.serper.dev/search",
            data=payload,
            headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        for result in data.get("organic", []):
            link = result.get("link", "")
            m = re.search(r"ebay\.com/b/[^/]+/(\d+)/", link)
            if m:
                cat_id = m.group(1)
                logger.info(f"  Serper category: '{title[:40]}' → {cat_id}")
                cache[cache_key] = cat_id
                _save_cat_cache(cache)
                return cat_id

    except Exception as e:
        logger.warning(f"  Serper category lookup failed for '{title[:40]}': {e}")

    cache[cache_key] = ""
    _save_cat_cache(cache)
    return ""


def _quantity_from_limit(limit_cell: str) -> str:
    """Listing quantity from the purchase_limit cell ("4/day" -> "4"); DEFAULT_QUANTITY if unset."""
    m = re.match(r"\s*(\d+)\s*(?:/\s*day)?\s*$", limit_cell or "", re.IGNORECASE)
    if m and int(m.group(1)) >= 1:
        return str(int(m.group(1)))
    return str(DEFAULT_QUANTITY)


def _load_config() -> dict:
    p = Path(__file__).parent.parent / "config" / "categories.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


def _ebay_category_id(title: str, category: str, config: dict) -> str:
    cat_config = config["categories"].get(category, {})
    cat_map = cat_config.get("ebay_category_map", {})
    title_lower = title.lower()
    for keyword, cat_id in cat_map.items():
        if keyword != "default" and keyword in title_lower:
            return str(cat_id)
    return str(cat_map.get("default", cat_config.get("ebay_category_id", "")))


_COLOR_WORDS = [
    # multi-word / specific first so "stainless steel" beats "steel"
    ("stainless steel", "Stainless Steel"), ("stainless", "Stainless Steel"),
    ("black", "Black"), ("white", "White"), ("gray", "Gray"), ("grey", "Gray"),
    ("silver", "Silver"), ("gold", "Gold"), ("red", "Red"), ("blue", "Blue"),
    ("green", "Green"), ("brown", "Brown"), ("beige", "Beige"), ("tan", "Tan"),
    ("cream", "Cream"), ("navy", "Blue"), ("pink", "Pink"), ("orange", "Orange"),
    ("yellow", "Yellow"), ("purple", "Purple"), ("charcoal", "Gray"), ("espresso", "Brown"),
]


def _color_from_title(title_lower: str) -> str:
    for word, color in _COLOR_WORDS:
        if re.search(rf"\b{word}\b", title_lower):
            return color
    return ""


def _fmt_dim(value: float) -> str:
    return f"{value:g} in"


def _fill_common_specifics(specifics: dict, title: str, category: str, brand: str,
                           notes: str, cat_config: dict) -> None:
    """
    Guarantees every key in the category's `ebay_required_specifics` is non-blank —
    eBay rejects listings with a missing required specific (error 21919303).
    Real values (Costco specs line in notes, title) win; otherwise a category default
    or NOT_APPLICABLE is used.
    """
    required = cat_config.get("ebay_required_specifics", [])
    title_lower = title.lower()
    fallbacks = []

    if "C:Brand" in required and not specifics.get("C:Brand"):
        specifics["C:Brand"] = "Unbranded"
        fallbacks.append("C:Brand")

    if "C:Color" in required and not specifics.get("C:Color"):
        color = _color_from_title(title_lower)
        specifics["C:Color"] = color or NOT_APPLICABLE
        if not color:
            fallbacks.append("C:Color")

    if "C:Model" in required:
        model = _parse_model_from_notes(notes) or specifics.get("C:Model", "")
        specifics["C:Model"] = model or NOT_APPLICABLE
        if not model:
            fallbacks.append("C:Model")

    dim_keys = {"C:Item Length": "length", "C:Item Width": "width", "C:Item Height": "height"}
    if any(k in required for k in dim_keys):
        dims = _parse_dimensions_from_notes(notes)
        source = "scraped"
        if not dims:
            dims, source = cat_config.get("default_dimensions"), "category default"
        for key, axis in dim_keys.items():
            if key not in required:
                continue
            specifics[key] = _fmt_dim(float(dims[axis])) if dims else NOT_APPLICABLE
        if source != "scraped":
            fallbacks.append(f"dimensions ({source})")

    # Last line of defence: any other required key still blank
    for key in required:
        if not specifics.get(key):
            specifics[key] = NOT_APPLICABLE
            fallbacks.append(key)

    if fallbacks:
        logger.info(f"  {title[:40]} — specifics via fallback: {', '.join(fallbacks)}")


def _infer_item_specifics(title: str, category: str, brand: str, cat_config: dict,
                          notes: str = "") -> dict:
    """
    Infers eBay item specifics from product title + category config (+ the "Costco specs:"
    line in notes). Returns a flat dict of C:FieldName → value; every key listed in the
    category's `ebay_required_specifics` is guaranteed non-blank.
    """
    title_lower = title.lower()
    specifics = {}

    if brand:
        specifics["C:Brand"] = brand

    if category == "Pharmacy":
        if any(w in title_lower for w in ["energy shot", "energy drink", "shot"]):
            specifics["C:Type"] = "Energy Shot"
        elif any(w in title_lower for w in ["fish oil", "omega"]):
            specifics["C:Type"] = "Fish Oil"
        elif any(w in title_lower for w in ["vitamin d", "vitamin c", "vitamin b"]):
            specifics["C:Type"] = "Vitamins & Minerals"
        elif "calcium" in title_lower:
            specifics["C:Type"] = "Calcium"
        elif "probiotic" in title_lower:
            specifics["C:Type"] = "Probiotics"
        elif "protein" in title_lower:
            specifics["C:Type"] = "Protein"
        elif any(w in title_lower for w in ["magnesium", "zinc", "iron"]):
            specifics["C:Type"] = "Vitamins & Minerals"
        else:
            specifics["C:Type"] = "Dietary Supplement"

        if any(w in title_lower for w in ["softgel", "soft gel", "softchew", "soft chew", "chew"]):
            specifics["C:Formulation"] = "Softgels"
        elif "tablet" in title_lower:
            specifics["C:Formulation"] = "Tablets"
        elif "capsule" in title_lower:
            specifics["C:Formulation"] = "Capsules"
        elif "liquid" in title_lower or "shot" in title_lower or "drink" in title_lower:
            specifics["C:Formulation"] = "Liquid"
        elif "powder" in title_lower:
            specifics["C:Formulation"] = "Powder"
        else:
            specifics["C:Formulation"] = "Other"

        count_match = re.search(r'(\d+)\s*(?:-count|count|bottles?|tablets?|capsules?|softgels?|ct\b)', title_lower)
        if count_match:
            specifics["C:Unit Count"] = count_match.group(1)

    elif category == "Jewelry":
        if "14kt" in title_lower or "14k" in title_lower:
            specifics["C:Metal"] = "14K Gold"
        elif "18kt" in title_lower or "18k" in title_lower:
            specifics["C:Metal"] = "18K Gold"
        elif "sterling" in title_lower or "silver" in title_lower:
            specifics["C:Metal"] = "Sterling Silver"
        elif "platinum" in title_lower:
            specifics["C:Metal"] = "Platinum"

        if "bracelet" in title_lower:
            specifics["C:Type"] = "Bracelet"
        elif "necklace" in title_lower or "chain" in title_lower:
            specifics["C:Type"] = "Necklace"
        elif "ring" in title_lower:
            specifics["C:Type"] = "Ring"
        elif "earring" in title_lower:
            specifics["C:Type"] = "Earrings"
        elif "pendant" in title_lower:
            specifics["C:Type"] = "Pendant"
        else:
            specifics["C:Type"] = "Other"

        style_map = {
            "paperclip": "Paperclip", "rope": "Rope", "popcorn": "Popcorn",
            "figaro": "Figaro", "cuban": "Cuban Link", "tennis": "Tennis",
            "heart": "Heart", "rolo": "Rolo", "charm": "Charm",
        }
        for kw, style in style_map.items():
            if kw in title_lower:
                specifics["C:Style"] = style
                break

    elif category == "Precious Metals":
        if "coin" in title_lower:
            specifics["C:Type"] = "Coin"
        else:
            specifics["C:Type"] = "Bar"

        if "gold" in title_lower:
            specifics["C:Metal"] = "Gold"
        elif "silver" in title_lower:
            specifics["C:Metal"] = "Silver"
        elif "platinum" in title_lower:
            specifics["C:Metal"] = "Platinum"

        purity = re.search(r'(\.9{3,4}|24\s*k|22\s*k)', title_lower)
        if purity:
            specifics["C:Purity"] = purity.group(1).upper()
        else:
            specifics["C:Purity"] = ".9999 Fine"

    elif category == "Outdoor Furniture":
        if "sectional" in title_lower:
            specifics["C:Type"] = "Sectional"
        elif "adirondack" in title_lower or "chair" in title_lower:
            specifics["C:Type"] = "Chair"
        elif "sofa" in title_lower or "loveseat" in title_lower:
            specifics["C:Type"] = "Sofa"
        elif "table" in title_lower:
            specifics["C:Type"] = "Table"
        elif "set" in title_lower:
            specifics["C:Type"] = "Patio Set"
        else:
            specifics["C:Type"] = "Patio Furniture"

        if "wicker" in title_lower or "rattan" in title_lower:
            specifics["C:Material"] = "Wicker/Rattan"
        elif "polywood" in title_lower or "hdpe" in title_lower:
            specifics["C:Material"] = "HDPE"
        elif "aluminum" in title_lower or "aluminium" in title_lower:
            specifics["C:Material"] = "Aluminum"
        elif "steel" in title_lower:
            specifics["C:Material"] = "Steel"
        elif "teak" in title_lower:
            specifics["C:Material"] = "Teak"
        else:
            specifics["C:Material"] = "Other"

    elif category == "Watches":
        specifics["C:Type"] = "Wristwatch"
        if "automatic" in title_lower:
            specifics["C:Movement"] = "Automatic"
        else:
            specifics["C:Movement"] = "Quartz"

    elif category == "Small Appliances":
        appliance_types = {
            "blender": "Blender", "air fryer": "Air Fryer", "vitamix": "Blender",
            "stand mixer": "Stand Mixer", "coffee": "Coffee Maker",
            "instant pot": "Pressure Cooker", "toaster": "Toaster",
            "food processor": "Food Processor", "juicer": "Juicer",
        }
        for kw, t in appliance_types.items():
            if kw in title_lower:
                specifics["C:Type"] = t
                break
        if "C:Type" not in specifics:
            specifics["C:Type"] = "Kitchen Appliance"

        model = re.search(r'\b([A-Z]{1,4}[\-]?[0-9]{3,6}[A-Z]?)\b', title)
        if model:
            specifics["C:Model"] = model.group(1)

    _fill_common_specifics(specifics, title, category, brand, notes, cat_config)
    return specifics


def generate_ebay_csv(rows_with_idx: list[tuple[int, list]], config: dict) -> str:
    """
    Builds and returns the CSV string for eBay Seller Hub upload.
    rows_with_idx: list of (sheet_row_number, row_data) tuples.
    """
    shipping = os.getenv("EBAY_SHIPPING_PROFILE", "")
    returns  = os.getenv("EBAY_RETURN_PROFILE", "")
    payment  = os.getenv("EBAY_PAYMENT_PROFILE", "")
    location = os.getenv("EBAY_LOCATION", "United States")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_EBAY_COLUMNS, lineterminator="\n")
    writer.writeheader()

    exported = 0
    skipped  = 0

    for sheet_row, row in rows_with_idx:
        seo_title   = _safe(row, _COL["seo_title"])
        ebay_price  = _safe(row, _COL["ebay_price"])
        description = _safe(row, _COL["description"])
        category    = _safe(row, _COL["category"])
        notes       = _safe(row, _COL["notes"])
        sku         = _safe(row, _COL["sku"])
        image_urls  = _safe(row, _COL["image_urls"])
        ebay_url    = _safe(row, _COL["ebay_url"])

        # Skip already-listed products (col Q has an eBay URL)
        if ebay_url.startswith("http"):
            skipped += 1
            continue

        # Skip if copy not yet generated
        if not seo_title:
            skipped += 1
            continue

        # Skip if no price — can't list without a price
        if not ebay_price:
            skipped += 1
            continue

        title      = _safe(row, _COL["title"])
        brand      = _parse_brand_from_notes(notes) or _brand_from_title(title)
        quantity   = _quantity_from_limit(_safe(row, _COL["purchase_limit"]))
        cat_id     = _ebay_category_id(title, category, config)
        cat_config = config["categories"].get(category, {})
        live_cat   = _serper_category_lookup(title)
        if live_cat:
            cat_id = live_cat
        cat_id = _CATEGORY_MIGRATIONS.get(cat_id, cat_id)
        logger.debug(f"  {title[:40]} → eBay category: {cat_id} ({category})")
        if not cat_id:
            logger.warning(f"  Skipping {title[:40]} — no eBay category ID configured for '{category}'")
            skipped += 1
            continue

        # PicURL: eBay accepts pipe-separated multiple image URLs
        pic_url = image_urls.replace(",", "|") if image_urls else PLACEHOLDER_IMAGE
        if pic_url == PLACEHOLDER_IMAGE:
            logger.info(f"  {title[:40]} — no images scraped, using placeholder. Replace in Seller Hub before publishing.")

        # Inline the HTML description as a single line (strip stray newlines)
        desc_clean = " ".join(description.split()) if description else ""

        # CustomLabel: use SKU if set, otherwise sheet row number for reference
        custom_label = sku if sku else f"ROW{sheet_row}"

        writer.writerow({
            "Action":              "Add",
            "SiteID":              "0",
            "Country":             "US",
            "Category":            cat_id,
            "Title":               seo_title[:80],
            "Subtitle":            "",
            "Format":              "FixedPrice",
            "StartPrice":          ebay_price,
            "Quantity":            quantity,
            "Duration":            "GTC",
            "Description":         desc_clean,
            "ConditionID":         "1000",
            "Location":            location,
            "PicURL":              pic_url,
            **{k: v for k, v in _infer_item_specifics(title, category, brand, cat_config, notes).items()
               if k in _EBAY_COLUMNS},
            "ShippingProfileName": shipping,
            "ReturnProfileName":   returns,
            "PaymentProfileName":  payment,
            "CustomLabel":         custom_label,
        })
        exported += 1

    logger.info(f"  CSV rows: {exported} exported, {skipped} skipped")
    return output.getvalue()


def export_approved_products() -> Path | None:
    """
    Reads APPROVED/WATCH products from the sheet, generates a Seller Hub CSV,
    saves it to data/exports/, and returns the file path.
    """
    config    = _load_config()
    business  = config["business"]
    service   = get_sheets_service()
    sheet_name = business["sheet_name"]
    start_row  = business["data_start_row"]
    end_row    = business["data_end_row"]

    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AZ{end_row}")

    eligible = []
    for idx, row in enumerate(all_data):
        if not row:
            continue
        status = _safe(row, _COL["status"])
        if status in _EXPORT_STATUSES:
            eligible.append((idx + start_row, row))

    if not eligible:
        logger.warning("No READY products found — nothing to export.")
        return None

    logger.info(f"Found {len(eligible)} eligible product(s) for export.")

    csv_text = generate_ebay_csv(eligible, config)

    export_dir = Path(__file__).parent.parent / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = export_dir / f"ebay_upload_{timestamp}.csv"

    out_path.write_text(csv_text, encoding="utf-8-sig")  # utf-8-sig = BOM for Excel compat
    logger.info(f"Saved: {out_path}")
    return out_path


if __name__ == "__main__":
    from loguru import logger as _log
    _log.remove()
    _log.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")

    path = export_approved_products()
    if path:
        print(f"\neBay upload file ready:\n  {path}")
        print("\nNext steps:")
        print("  1. Open eBay Seller Hub → Reports → Uploads")
        print("  2. Upload this CSV file")
        print("  3. Add photos to each listing via eBay's image uploader")
        print("  4. Review & activate listings in Seller Hub")
        print("\nNote: Set EBAY_SHIPPING_PROFILE / EBAY_RETURN_PROFILE / EBAY_PAYMENT_PROFILE")
        print("      in .env to match your Seller Hub Business Policies before uploading.")
    else:
        print("Nothing exported.")