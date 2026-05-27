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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from loguru import logger

load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import get_sheets_service, read_sheet


# ── Column indices (0-based, matching A=0) ───────────────────────────────────
# Source of truth: config/col_map.yaml — update there first, then here.
# Current visible range A–Z (indices 0–25), hidden AA–AV (indices 26–47).
_COL = {
    "status":       0,   # A
    "demand_score": 1,   # B
    "title":        2,   # C
    "category":     3,   # D
    "ebay_price":   7,   # H
    "notes":        47,  # AV — full research narrative; contains purchase_limit / brand hints
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
    "C:Material",
    "C:Type",
    "ShippingProfileName",
    "ReturnProfileName",
    "PaymentProfileName",
    "CustomLabel",
]


def _safe(lst: list, i: int, default: str = "") -> str:
    return str(lst[i]).strip() if i < len(lst) else default


def _parse_brand_from_notes(notes: str) -> str:
    """Extract brand written by costco_scraper into notes field."""
    m = re.search(r"brand[:\s]+([^\n|]+)", notes, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _parse_quantity_from_notes(notes: str) -> str:
    """Extract purchase_limit from notes if present, default to 1."""
    m = re.search(r"list max (\d+) on eBay", notes, re.IGNORECASE)
    return m.group(1) if m else "1"


def _load_config() -> dict:
    p = Path(__file__).parent.parent / "config" / "categories.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


def _ebay_category_id(category: str, config: dict) -> str:
    cat_config = config["categories"].get(category, {})
    return str(cat_config.get("ebay_category_id", ""))


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

        brand    = _parse_brand_from_notes(notes)
        quantity = _parse_quantity_from_notes(notes)
        cat_id   = _ebay_category_id(category, config)

        # PicURL: eBay accepts pipe-separated multiple image URLs
        pic_url = image_urls.replace("|", "|") if image_urls else ""

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
            "C:Brand":             brand,
            "C:Material":          "",
            "C:Type":              "",
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
