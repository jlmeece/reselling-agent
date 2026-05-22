# Legend Column Reference — Design Spec
**Date:** 2026-05-22  
**Status:** Approved

## Overview

The Legend tab's "COLUMN QUICK REFERENCE" currently documents 12 of 26 visible columns and none of the 22 hidden ones. This spec covers two improvements:

1. **Full column reference in Legend** — all 48 columns (A–AV) documented, split into VISIBLE and HIDDEN sections with appropriate styling.
2. **Header labels for hidden columns** — Product Tracker row 3 gets labels for AA–AV so they have proper names if ever unhidden.

---

## Architecture

**One file modified:** `tools/sheet_formatter.py`

| Area | Change |
|------|--------|
| `HEADER_LABELS` | Extended from 26 to 48 entries (AA–AV added) |
| `_write_header_text` | No change — already writes `HEADER_LABELS` to row 3; extending the list auto-handles hidden cols |
| `_ensure_legend_tab` | "COLUMN QUICK REFERENCE" section replaced with two new sections |

No changes to `setup_sheet.py`, `col_map.yaml`, or any other file.

---

## HEADER_LABELS Extension

```python
HEADER_LABELS = [
    # A–Z (unchanged)
    "STATUS", "TIER", "PRODUCT TITLE", "CATEGORY", "PLATFORM", "STOCK",
    "COST $", "eBay PRICE", "NET PROFIT", "MARGIN", "SOLD 90d", "AVG eBay $",
    "ACTIVE", "COMP SCORE", "LAST CHECKED", "PRICE CHG", "eBay LISTING",
    "COSTCO URL", "RE-EVAL DATE", "TIER SUMMARY", "UNITS SOLD", "SUGG. PRICE",
    "PURCH. LIMIT", "SALE", "SHIP COST", "TOTAL COST",
    # AA–AV (new)
    "SKU", "FEE RATE", "eBay FEES", "SHIP COST $", "FULFILLMENT",
    "TAX EST.", "SITE PROFIT", "AD BUDGET", "SEO TITLE", "BULLETS",
    "DESCRIPTION", "REDIRECT MSG", "META DESC", "KEYWORDS", "ALT TEXT",
    "GOOGLE HL", "GOOGLE DESC", "META TEXT", "META HL", "IMAGE URLS",
    "PERF SCORE", "FULL NOTES",
]
```

Y = "SHIP COST" (badge display — e.g. "✓ FREE"); AD = "SHIP COST $" (raw dollar value). Distinct labels to avoid confusion.

Hidden col headers are written to row 3 but stay hidden — visible only if columns are unhidden.

---

## Legend Structure

### VISIBLE COLUMNS (A–Z) section

4-column table: **COL | HEADER | WHAT IT STORES | NOTES**

All 26 visible columns documented. Formula columns get "formula" in the NOTES column (styled distinctly — no per-row "do not overwrite" repetition). Section note at top: *"Formula columns are maintained by the sheet — do not overwrite."*

| COL | HEADER | WHAT IT STORES | NOTES |
|-----|--------|----------------|-------|
| A | STATUS | Current pipeline stage. Use the dropdown. | |
| B | TIER | 0–10 composite score. Green ≥7, Yellow 4–7, Red <4. | |
| C | PRODUCT TITLE | Product name from Costco. | |
| D | CATEGORY | Category (Precious Metals, Jewelry, etc.). | |
| E | PLATFORM | eBay / Site / Both — set when listed. | |
| F | STOCK | In Stock / Available (2/day limit) / OUT OF STOCK. | |
| G | COST $ | What you pay at Costco. | |
| H | eBay PRICE | Your eBay listing price. Agent sets once; edit freely. | |
| I | NET PROFIT | eBay Price − Cost − Fees − Shipping. | formula |
| J | MARGIN | Net Profit / eBay Price. Target ≥ 10%. | formula |
| K | SOLD 90d | eBay sold listings in the last 90 days. | |
| L | AVG eBay $ | Average eBay sold price across comps. | |
| M | ACTIVE | Active competing eBay listings count. | |
| N | COMP SCORE | Active listings ÷ 90d sold. Low < 2×, Med < 10×, High ≥ 10×. | formula |
| O | LAST CHECKED | Timestamp of last agent run for this product. | |
| P | PRICE CHG | Flag: Costco price moved since last check. | |
| Q | eBay LISTING | Paste live eBay URL here → triggers ACTIVE monitoring. | |
| R | COSTCO URL | Source product link on Costco.com. | |
| S | RE-EVAL DATE | PAUSED items: date agent will re-research. | |
| T | TIER SUMMARY | Short one-liner: [T2 \| Score 8.2 \| Sugg: $899 \| margin 18%]. | |
| U | UNITS SOLD | Units sold (manually entered or future eBay API). | |
| V | SUGG. PRICE | Agent's one-time recommended eBay price. Do not overwrite. | |
| W | PURCH. LIMIT | Costco daily buy limit (precious metals: 2/day, else blank). | |
| X | SALE | 🔥 -$150 ends 5/31 badge when product is on sale at Costco. | |
| Y | SHIP COST | ✓ FREE (teal) or $12.99 ship (amber). Blank if unknown. | |
| Z | TOTAL COST | Costco cost + shipping. All-in delivered price. | formula |

### HIDDEN COLUMNS (AA–AV) section

3-column table: **COL | KEY | WHAT IT STORES**

Section divider uses `STATS_BG` (mid-navy, lighter than the visible section's `HDR_BG`) — signals "reference only, not for daily use." Section note: *"Agent-managed. Do not edit directly."*

| COL | KEY | WHAT IT STORES |
|-----|-----|----------------|
| AA | SKU | Costco product SKU. |
| AB | FEE RATE | eBay fee rate applied to this product (default 13.25%). |
| AC | eBay FEES | formula: eBay Price × Fee Rate. |
| AD | SHIP COST $ | Shipping cost in dollars (raw value behind the Y badge). |
| AE | FULFILLMENT | Fulfillment cost if applicable. |
| AF | TAX EST. | formula: Costco Cost × 8.25%. |
| AG | SITE PROFIT | formula: eBay Price × 0.90 − Cost. |
| AH | AD BUDGET | formula: Net Profit × 15%. |
| AI | SEO TITLE | Optimized listing title for eBay / site. |
| AJ | BULLETS | Bullet points for eBay listing description. |
| AK | DESCRIPTION | Full listing description. |
| AL | REDIRECT MSG | Message shown if product is no longer available. |
| AM | META DESC | SEO meta description. |
| AN | KEYWORDS | Search keywords for eBay / Google. |
| AO | ALT TEXT | Image alt text for accessibility / SEO. |
| AP | GOOGLE HL | Google Shopping headline. |
| AQ | GOOGLE DESC | Google Shopping description. |
| AR | META TEXT | Additional meta text. |
| AS | META HL | Meta headline. |
| AT | IMAGE URLS | Comma-separated Costco image URLs. |
| AU | PERF SCORE | Composite performance score (0–10) written by rotation engine. |
| AV | FULL NOTES | Full research narrative, eBay comps, community signals. |

---

## Styling

| Element | Style |
|---------|-------|
| "VISIBLE COLUMNS" divider | `HDR_BG` (existing dark navy) — matches all other section dividers |
| "HIDDEN COLUMNS" divider | `STATS_BG` (mid-navy) — visually muted vs. visible section |
| "formula" tag in NOTES col | Existing `_rgb("B0BEC5")` italic style — called out without repeating warning |
| Section notes | Italic row below each divider, `STATS_BG` background |
| Column widths (Legend) | A: 80px (col letter), B: 140px (header/key), C: 380px (description), D: 90px (notes tag) |

The existing Legend column widths (140 / 520 / 280 / 80) are adjusted to fit the new 4-col structure. Col A narrows (just "A" or "AA"), col B widens slightly for the header label, col C takes the description.

---

## What Gets Removed

The existing "COLUMN QUICK REFERENCE" section (12 partial entries, no hidden cols) is replaced entirely by the two new sections. No other Legend content changes.
