"""
Tool: sheet_formatter
Builds the Product Tracker dashboard and supporting tabs.

Layout: STATUS and TIER SCORE are columns A and B — always visible.
Columns A–D are frozen so they stay in view while scrolling right.
Columns AA–AV are hidden (SKU, fees, SEO copy, formulas, ad copy, image URLs, full notes).
Col Z (TOTAL COST) is visible — formula =IFERROR(G+AD,G).

Run via: python agents/setup_sheet.py
Safe to re-run — clears formatting before applying fresh.
"""

import os
from loguru import logger


# ── Colors ─────────────────────────────────────────────────────────────────────

def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


TITLE_BG  = _rgb("0D1B2A")   # deepest navy
STATS_BG  = _rgb("16304F")   # mid navy
HDR_BG    = _rgb("1E3A5F")   # column header navy
HDR_FG    = _rgb("FFFFFF")
BORDER_CLR = _rgb("616161")  # solid grid border (dark grey — visible on colored backgrounds)

STATUS_COLORS = {
    "PENDING":        _rgb("FFF9C4"),   # yellow    — new, awaiting review
    "APPROVED":       _rgb("DCEDC8"),   # green     — Jordan approved, copy generating
    "READY":          _rgb("B2DFDB"),   # teal      — copy done, export to list
    "ACTIVE":         _rgb("F1F8E9"),   # pale green — live listing, monitored
    "WATCH":          _rgb("BBDEFB"),   # blue      — Tier 2, needs more data
    "PAUSED_OOS":     _rgb("FFE0B2"),   # orange    — out of stock at Costco
    "PAUSED_MARGIN":  _rgb("FFE0B2"),   # orange    — margin below threshold
    "PAUSED_DEMAND":  _rgb("F3E5F5"),   # lavender  — low demand / high competition
    "PAUSED_SEASONAL":_rgb("E8EAF6"),   # indigo    — off-season hold
}

TIER1_BG = _rgb("A5D6A7")
TIER2_BG = _rgb("FFF176")
TIER3_BG = _rgb("EF9A9A")

# Comp saturation colors (col N): Low / Medium / High
COMP_LOW_BG  = _rgb("C8E6C9")   # green   — < 2× active:sold
COMP_MED_BG  = _rgb("FFF59D")   # yellow  — 2× to 10×
COMP_HIGH_BG = _rgb("FFAB91")   # orange  — > 10×


# ── Column config ───────────────────────────────────────────────────────────────

HEADER_LABELS = [
    # A–Z  (visible, indices 0–25)
    "STATUS",          # A  0
    "TIER",            # B  1
    "PRODUCT TITLE",   # C  2
    "CATEGORY",        # D  3
    "PLATFORM",        # E  4
    "STOCK",           # F  5
    "COST $",          # G  6
    "eBay PRICE",      # H  7
    "NET PROFIT",      # I  8
    "MARGIN",          # J  9
    "SOLD 90d",        # K  10
    "AVG eBay $",      # L  11
    "ACTIVE",          # M  12 — competing listings count
    "COMP SCORE",      # N  13 — active÷sold ratio (Low<2 / Med<10 / High≥10)
    "LAST CHECKED",    # O  14
    "PRICE CHG",       # P  15
    "eBay LISTING",    # Q  16
    "COSTCO URL",      # R  17
    "RE-EVAL DATE",    # S  18
    "TIER SUMMARY",    # T  19 — short: [T2 | Score 8.2 | Sugg: $899 | margin 18%]
    "UNITS SOLD",      # U  20 — manually entered or future eBay API
    "SUGG. PRICE",     # V  21 — agent's recommended eBay price (written once)
    "PURCH. LIMIT",    # W  22 — units/day cap (precious metals) or blank
    "SALE",            # X  23 — 🔥 -$150 ends 5/31 (blank if not on sale)
    "SHIP COST",       # Y  24 — ✓ FREE or $12.99 ship (blank if unknown)
    "TOTAL COST",      # Z  25 — formula =IFERROR(G+AD,G) — Costco cost + shipping
    # AA–AV  (hidden, indices 26–47)
    "SKU",             # AA 26
    "FEE RATE",        # AB 27
    "eBay FEES",       # AC 28
    "SHIP COST $",     # AD 29
    "FULFILLMENT",     # AE 30
    "TAX EST.",        # AF 31
    "SITE PROFIT",     # AG 32
    "AD BUDGET",       # AH 33
    "SEO TITLE",       # AI 34
    "BULLETS",         # AJ 35
    "DESCRIPTION",     # AK 36
    "REDIRECT MSG",    # AL 37
    "META DESC",       # AM 38
    "KEYWORDS",        # AN 39
    "ALT TEXT",        # AO 40
    "GOOGLE HL",       # AP 41
    "GOOGLE DESC",     # AQ 42
    "META TEXT",       # AR 43
    "META HL",         # AS 44
    "IMAGE URLS",      # AT 45
    "PERF SCORE",      # AU 46
    "FULL NOTES",      # AV 47
]

COLUMN_WIDTHS = {
    0:  95,    # A: status
    1:  60,    # B: tier score
    2:  280,   # C: title
    3:  90,    # D: category
    4:  75,    # E: platform
    5:  110,   # F: stock
    6:  75,    # G: cost
    7:  80,    # H: ebay price
    8:  80,    # I: net profit
    9:  75,    # J: margin
    10: 70,    # K: sold 90d
    11: 80,    # L: avg price
    12: 75,    # M: active count
    13: 85,    # N: comp score
    14: 120,   # O: last checked
    15: 75,    # P: price change
    16: 60,    # Q: ebay listing url
    17: 60,    # R: costco url
    18: 90,    # S: re-eval date
    19: 180,   # T: tier summary (short one-liner)
    20: 70,    # U: units sold
    21: 90,    # V: suggested price
    22: 110,   # W: purchase limit
    23: 130,   # X: sale badge
    24: 90,    # Y: ship cost badge (wider — "$12.99 ship" needs more room than "✓ FREE")
    25: 110,   # Z: total cost
}

VISIBLE_COLS  = 26    # A–Z
TOTAL_COLS    = 48    # A–AV
HIDDEN_START  = 26    # AA onwards (index 26 = col AA)
FROZEN_COLS   = 4     # A–D always visible

SALE_COL_IDX  = 23    # X — orange badge when non-empty
SHIP_COL_IDX  = 24    # Y — teal badge (FREE) or amber badge (paid ship)


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _get_sheet_meta(service, spreadsheet_id):
    return service.spreadsheets().get(spreadsheetId=spreadsheet_id, includeGridData=False).execute()


def _get_tab_id(meta, sheet_name):
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == sheet_name:
            return sheet["properties"]["sheetId"]
    return None


def _cell_range(tab_id, start_row, end_row, start_col=0, end_col=TOTAL_COLS):
    return {
        "sheetId": tab_id,
        "startRowIndex": start_row, "endRowIndex": end_row,
        "startColumnIndex": start_col, "endColumnIndex": end_col,
    }


def _border_style(color=None):
    c = color or BORDER_CLR
    return {"style": "SOLID", "width": 1, "color": c}


def _row_border_request(tab_id, start_row, end_row):
    """Solid borders on every side of every data cell — produces a clean grid."""
    return {
        "updateBorders": {
            "range": _cell_range(tab_id, start_row, end_row, 0, VISIBLE_COLS),
            "top":             _border_style(),
            "bottom":          _border_style(),
            "left":            _border_style(),
            "right":           _border_style(),
            "innerHorizontal": _border_style(),
            "innerVertical":   _border_style(),
        }
    }


def _conditional_row_color(tab_id, status_value, bg_color, data_start_row, max_row=1000):
    # Range MUST start at data_start_row - 1 (0-indexed) so it includes row 4.
    # Formula references $A{data_start_row} (the top-left cell of the range).
    formula = f'=$A{data_start_row}="{status_value}"'
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_cell_range(tab_id, data_start_row - 1, max_row)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": bg_color},
                },
            },
            "index": 0,
        }
    }


def _conditional_tier_color(tab_id, formula, bg_color, data_start_row):
    # Range starts at row index data_start_row - 1 (1-indexed row 4) — col B only.
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_cell_range(tab_id, data_start_row - 1, 1000, 1, 2)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": bg_color, "textFormat": {"bold": True}},
                },
            },
            "index": 0,
        }
    }


def _clear_formatting(service, spreadsheet_id, tab_id):
    meta = _get_sheet_meta(service, spreadsheet_id)
    cleanup = []
    for sheet in meta["sheets"]:
        if sheet["properties"]["sheetId"] != tab_id:
            continue
        for br in sheet.get("bandedRanges", []):
            cleanup.append({"deleteBanding": {"bandedRangeId": br["bandedRangeId"]}})
        cf_count = len(sheet.get("conditionalFormats", []))
        for i in range(cf_count - 1, -1, -1):
            cleanup.append({"deleteConditionalFormatRule": {"sheetId": tab_id, "index": i}})
        for fv in sheet.get("filterViews", []):
            cleanup.append({"deleteFilterView": {"filterId": fv["filterViewId"]}})
    if cleanup:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": cleanup}
        ).execute()


# ── Tab management ─────────────────────────────────────────────────────────────

def _delete_tabs(service, spreadsheet_id, names_to_delete, meta):
    requests = []
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] in names_to_delete:
            requests.append({"deleteSheet": {"sheetId": sheet["properties"]["sheetId"]}})
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()
        logger.info(f"  Deleted tabs: {names_to_delete}")


def _ensure_images_tab(service, spreadsheet_id, meta):
    """Create Images tab if it doesn't exist."""
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if "Images" not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": "Images", "index": 1}}}]},
        ).execute()
        logger.info("  Created Images tab.")

    # Write headers
    headers = [["PRODUCT TITLE", "STATUS", "IMAGE 1", "IMAGE 2", "IMAGE 3", "IMAGE 4", "IMAGE 5"]]
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="'Images'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": headers},
    ).execute()

    # Format Images tab header row
    meta2 = _get_sheet_meta(service, spreadsheet_id)
    img_tab_id = _get_tab_id(meta2, "Images")
    if img_tab_id is not None:
        img_requests = [
            {"repeatCell": {
                "range": {"sheetId": img_tab_id, "startRowIndex": 0, "endRowIndex": 1,
                           "startColumnIndex": 0, "endColumnIndex": 7},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": HDR_BG,
                    "textFormat": {"foregroundColor": HDR_FG, "bold": True, "fontSize": 9},
                    "horizontalAlignment": "CENTER",
                }},
                "fields": "userEnteredFormat",
            }},
            # Freeze header row
            {"updateSheetProperties": {
                "properties": {"sheetId": img_tab_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
            # Column widths: title wide, status medium, images wide
            {"updateDimensionProperties": {
                "range": {"sheetId": img_tab_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 280}, "fields": "pixelSize",
            }},
            {"updateDimensionProperties": {
                "range": {"sheetId": img_tab_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                "properties": {"pixelSize": 80}, "fields": "pixelSize",
            }},
        ]
        for i in range(2, 7):
            img_requests.append({"updateDimensionProperties": {
                "range": {"sheetId": img_tab_id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": 320}, "fields": "pixelSize",
            }})
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": img_requests}
        ).execute()


# ── Filter views ───────────────────────────────────────────────────────────────

def _create_filter_views(service, spreadsheet_id, tab_id, data_start_row):
    """Create 4 preset filter views for common workflow states."""

    def _filter_view(name, criteria):
        return {
            "addFilterView": {
                "filter": {
                    "title": name,
                    "range": _cell_range(tab_id, data_start_row - 1, 1000),
                    "criteria": criteria,
                }
            }
        }

    def _values_condition(*values):
        return {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": v} for v in values]}}

    all_statuses = [
        "PENDING", "APPROVED", "READY", "ACTIVE", "WATCH",
        "PAUSED_OOS", "PAUSED_MARGIN", "PAUSED_DEMAND", "PAUSED_SEASONAL",
    ]

    def _hidden_except(*keep):
        return {"hiddenValues": [s for s in all_statuses if s not in keep]}

    requests = [
        # Col A (index 0) = STATUS
        # "Action needed" = paused states that require immediate attention
        _filter_view("ACTION — Needs Attention", {
            "0": _hidden_except("PAUSED_OOS", "PAUSED_MARGIN")
        }),
        # Queue = items Jordan still needs to review / approve
        _filter_view("QUEUE — Pending Review", {
            "0": _hidden_except("PENDING")
        }),
        # Active inventory = live or export-ready
        _filter_view("ACTIVE — Live Inventory", {
            "0": _hidden_except("ACTIVE", "READY")
        }),
        # Bench = all paused + watch (not currently selling)
        _filter_view("BENCH — Paused & Watch", {
            "0": _hidden_except("PAUSED_OOS", "PAUSED_MARGIN", "PAUSED_DEMAND", "PAUSED_SEASONAL", "WATCH")
        }),
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
    logger.info("  Filter views created.")


# ── Status dropdown validation ─────────────────────────────────────────────────

def _add_status_dropdown(tab_id, data_start_row):
    return {
        "setDataValidation": {
            "range": _cell_range(tab_id, data_start_row - 1, 1000, 0, 1),
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": s}
                        for s in [
                            "PENDING", "APPROVED", "READY", "ACTIVE", "WATCH",
                            "PAUSED_OOS", "PAUSED_MARGIN", "PAUSED_DEMAND", "PAUSED_SEASONAL",
                        ]
                    ],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    }


# ── Main setup ─────────────────────────────────────────────────────────────────

def setup_dashboard(service, sheet_name, data_start_row=4):
    """
    Full dashboard rebuild. Safe to re-run — clears formatting first.
    data_start_row: 1-indexed row where product data begins.
    """
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    meta    = _get_sheet_meta(service, spreadsheet_id)
    tab_id  = _get_tab_id(meta, sheet_name)
    if tab_id is None:
        raise ValueError(f"Tab '{sheet_name}' not found.")

    header_row_idx = data_start_row - 2   # 0-indexed (row 3 = idx 2)
    data_row_idx   = data_start_row - 1   # 0-indexed (row 4 = idx 3)

    logger.info("Rebuilding dashboard...")

    # 1. Delete stale tabs
    _delete_tabs(service, spreadsheet_id, {"Assumptions", "Claude Prompts", "Image Download Log"}, meta)

    # 2. Ensure Images tab exists
    _ensure_images_tab(service, spreadsheet_id, meta)

    # 3. Clear old formatting + filter views on Product Tracker
    _clear_formatting(service, spreadsheet_id, tab_id)

    # 4. Unmerge ALL existing merged cells in the entire sheet (separate API call).
    #    Column freeze validation checks the sheet's current state — any merge
    #    spanning the freeze boundary causes a 400. We clear everything first.
    meta_fresh = _get_sheet_meta(service, spreadsheet_id)
    for s in meta_fresh["sheets"]:
        if s["properties"]["sheetId"] == tab_id:
            merges = s.get("merges", [])
            break
    else:
        merges = []

    if merges:
        unmerge_reqs = [{"unmergeCells": {"range": {
            "sheetId": tab_id,
            "startRowIndex": m["startRowIndex"], "endRowIndex": m["endRowIndex"],
            "startColumnIndex": m["startColumnIndex"], "endColumnIndex": m["endColumnIndex"],
        }}} for m in merges]
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": unmerge_reqs}
        ).execute()
        logger.info(f"  Unmerged {len(merges)} ranges.")

    requests = []

    # 5. Clear basic filter (will re-add at end so dropdown arrows show in header)
    requests.append({"clearBasicFilter": {"sheetId": tab_id}})

    # 5b. Force-resize all data rows to a uniform height (28px) so titles clip
    #     instead of expanding rows 10-12 etc. Applied before per-row borders.
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": tab_id, "dimension": "ROWS",
                  "startIndex": data_row_idx, "endIndex": 1000},
        "properties": {"pixelSize": 28}, "fields": "pixelSize",
    }})

    # 6. Freeze rows 1–3 + columns A–D, and expand grid to TOTAL_COLS columns
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": tab_id,
                "gridProperties": {
                    "frozenRowCount": data_row_idx,
                    "frozenColumnCount": FROZEN_COLS,
                    "columnCount": TOTAL_COLS,
                },
            },
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.columnCount",
        }
    })

    # 7. Title row (row 1) — no merge (avoids freeze conflict; text overflows naturally)
    requests.append({
        "repeatCell": {
            "range": _cell_range(tab_id, 0, 1),
            "cell": {"userEnteredFormat": {
                "backgroundColor": TITLE_BG,
                "textFormat": {"foregroundColor": HDR_FG, "bold": True, "fontSize": 14},
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat",
        }
    })

    # 8. Stats row (row 2) — no merge
    requests.append({
        "repeatCell": {
            "range": _cell_range(tab_id, 1, 2),
            "cell": {"userEnteredFormat": {
                "backgroundColor": STATS_BG,
                "textFormat": {"foregroundColor": _rgb("B0BEC5"), "fontSize": 9, "italic": True},
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat",
        }
    })

    # 8. Header row (row 3)
    requests.append({
        "repeatCell": {
            "range": _cell_range(tab_id, header_row_idx, header_row_idx + 1),
            "cell": {"userEnteredFormat": {
                "backgroundColor": HDR_BG,
                "textFormat": {"foregroundColor": HDR_FG, "bold": True, "fontSize": 9},
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                "wrapStrategy": "CLIP",
            }},
            "fields": "userEnteredFormat",
        }
    })

    # 9. Row heights
    for start, end, px in [(0, 1, 42), (1, 2, 24), (header_row_idx, header_row_idx + 1, 28)]:
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": px}, "fields": "pixelSize",
        }})

    # 10. Column widths (visible cols)
    for col_idx, width in COLUMN_WIDTHS.items():
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                      "startIndex": col_idx, "endIndex": col_idx + 1},
            "properties": {"pixelSize": width}, "fields": "pixelSize",
        }})

    # 11. Hide columns AA–AV (index 26–47)
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                  "startIndex": HIDDEN_START, "endIndex": TOTAL_COLS},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser",
    }})

    # 12. Data rows base style — CLIP so text doesn't bleed into neighbor cells
    requests.append({
        "repeatCell": {
            "range": _cell_range(tab_id, data_row_idx, 1000, 0, VISIBLE_COLS),
            "cell": {"userEnteredFormat": {
                "textFormat": {"fontSize": 9},
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "CLIP",
                "padding": {"top": 2, "bottom": 2, "left": 5, "right": 5},
            }},
            "fields": "userEnteredFormat(textFormat,verticalAlignment,wrapStrategy,padding)",
        }
    })
    # T (index 19) is now a short one-liner; CLIP like other visible cols (already covered above)

    # 13. Solid row borders (every row in data area)
    requests.append(_row_border_request(tab_id, data_row_idx, 1000))

    # 14. Alternating row banding (subtle — borders are the primary separator).
    # NO headerColor: the data range has no internal header. Setting one would
    # override the status color on the first data row (row 4).
    requests.append({"addBanding": {"bandedRange": {
        "range": _cell_range(tab_id, data_row_idx, 1000),
        "rowProperties": {
            "firstBandColor":  _rgb("FFFFFF"),
            "secondBandColor": _rgb("F8F9FA"),
        },
    }}})

    # 15. Status dropdown on col A
    requests.append(_add_status_dropdown(tab_id, data_start_row))

    # 16. Conditional formatting — row colors by STATUS (col A)
    for status in [
        "PAUSED_OOS", "PAUSED_MARGIN", "PAUSED_DEMAND", "PAUSED_SEASONAL",
        "ACTIVE", "READY", "WATCH", "APPROVED", "PENDING",
    ]:
        requests.append(_conditional_row_color(tab_id, status, STATUS_COLORS[status], data_row_idx))

    # 17. Conditional formatting — Tier score cell (col B)
    # Formula reference uses data_start_row (the FIRST cell of the rule range).
    # Sheets evaluates the formula relative to that anchor, then auto-shifts per row.
    score_ref = f"$B{data_start_row}"
    requests.append(_conditional_tier_color(tab_id, f"={score_ref}>=7", TIER1_BG, data_row_idx))
    requests.append(_conditional_tier_color(
        tab_id, f"=AND({score_ref}>=4,{score_ref}<7)", TIER2_BG, data_row_idx))
    requests.append(_conditional_tier_color(
        tab_id, f"=AND(ISNUMBER({score_ref}),{score_ref}<4)", TIER3_BG, data_row_idx))

    # 17b. Conditional formatting — COMP SCORE cell (col N, index 13)
    comp_ref = f"$N{data_start_row}"
    for formula, bg in [
        (f"=AND(ISNUMBER({comp_ref}),{comp_ref}<2)",                COMP_LOW_BG),
        (f"=AND(ISNUMBER({comp_ref}),{comp_ref}>=2,{comp_ref}<10)", COMP_MED_BG),
        (f"=AND(ISNUMBER({comp_ref}),{comp_ref}>=10)",              COMP_HIGH_BG),
    ]:
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [_cell_range(tab_id, data_row_idx, 1000, 13, 14)],
                    "booleanRule": {
                        "condition": {"type": "CUSTOM_FORMULA",
                                       "values": [{"userEnteredValue": formula}]},
                        "format": {"backgroundColor": bg, "textFormat": {"bold": True}},
                    },
                },
                "index": 0,
            }
        })

    # 17c. Conditional formatting — SALE col (X = index 23): amber bg when non-empty
    sale_ref = f"$X{data_start_row}"
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_cell_range(tab_id, data_row_idx, 1000, SALE_COL_IDX, SALE_COL_IDX + 1)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                   "values": [{"userEnteredValue": f'={sale_ref}<>""'}]},
                    "format": {"backgroundColor": _rgb("FFB300"),
                               "textFormat": {"bold": True, "foregroundColor": _rgb("1A1A1A")}},
                },
            },
            "index": 0,
        }
    })

    # 17d. Conditional formatting — SHIP COST col (Y = index 24):
    #   teal when "✓ FREE", amber when paid shipping amount shown
    ship_ref = f"$Y{data_start_row}"
    # Rule: teal bg when cell contains FREE
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_cell_range(tab_id, data_row_idx, 1000, SHIP_COL_IDX, SHIP_COL_IDX + 1)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                   "values": [{"userEnteredValue": f'=ISNUMBER(SEARCH("FREE",{ship_ref}))'}]},
                    "format": {"backgroundColor": _rgb("00897B"),
                               "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")}},
                },
            },
            "index": 0,
        }
    })
    # Rule: amber bg when cell contains "ship" (paid shipping amount)
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_cell_range(tab_id, data_row_idx, 1000, SHIP_COL_IDX, SHIP_COL_IDX + 1)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                   "values": [{"userEnteredValue": f'=ISNUMBER(SEARCH("ship",{ship_ref}))'}]},
                    "format": {"backgroundColor": _rgb("F57C00"),
                               "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")}},
                },
            },
            "index": 0,
        }
    })

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
    logger.info("  Formatting applied.")

    # Write text (separate values call)
    _write_header_text(service, spreadsheet_id, sheet_name, data_start_row)

    # Create filter views
    _create_filter_views(service, spreadsheet_id, tab_id, data_start_row)

    # Add basic filter (dropdown arrows on every header column)
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "setBasicFilter": {
                "filter": {
                    "range": _cell_range(tab_id, header_row_idx, 1000, 0, VISIBLE_COLS),
                }
            }
        }]},
    ).execute()
    logger.info("  Header filter dropdowns enabled.")

    # Ensure Legend tab exists / refresh definitions
    _ensure_legend_tab(service, spreadsheet_id)

    # Rebuild Summary tab with current sheet data
    all_data = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A4:AV1000",
    ).execute().get("values", [])
    refresh_summary_tab(service, sheet_name, all_data=all_data)

    logger.info("Dashboard setup complete.")


# ── Legend tab ─────────────────────────────────────────────────────────────────

STATUS_LEGEND = [
    ["STATUS",           "MEANING",                                                                              "WHEN IT'S SET",                                                          "NEXT ACTION"],
    ["PENDING",          "Newly discovered or Tier 1 scored — awaiting your review.",                            "Auto — set by discovery or researcher (Tier 1 result).",                "Review in sheet. Change to APPROVED to greenlight, or leave for next run."],
    ["APPROVED",         "You approved it. Agent is verifying stock and generating listing copy.",               "Manual — you change col A to APPROVED after reviewing.",                 "Agent generates copy, verifies Costco stock, then auto-sets to READY."],
    ["READY",            "Copy complete and stock verified. Export CSV and list on eBay.",                       "Auto — set by daily sweep when APPROVED product has copy + stock OK.",   "Run python tools/ebay_export.py. Upload CSV to eBay Seller Hub. Add photos."],
    ["ACTIVE",           "Live listing on eBay or your site. Highest monitoring priority.",                      "Auto — set when eBay URL is filled in col Q, or manual.",               "Agent checks stock/price 3x/day. Issues trigger PAUSED_OOS or alert."],
    ["WATCH",            "Tier 2 — promising but needs more data. Monitored weekly.",                            "Auto — set by researcher when score is 4.0–6.9.",                        "Agent re-researches weekly. If conditions improve, promotes to PENDING."],
    ["PAUSED_OOS",       "Costco is out of stock. eBay listing should be paused to avoid unfilled orders.",     "Auto — set by active monitor when Costco page shows OUT OF STOCK.",      "Pause your eBay listing manually. Agent checks daily and moves to WATCH when restocked."],
    ["PAUSED_MARGIN",    "Margin has fallen below the minimum threshold. Not profitable at current prices.",    "Auto — set by active monitor when net_margin < 10%.",                    "Lower your eBay price or wait for Costco cost to drop. Agent promotes to WATCH when margin recovers."],
    ["PAUSED_DEMAND",    "Low eBay demand or extreme competition (e.g. 26x comp score). Not worth listing.",   "Auto — set by researcher for Tier 3 products.",                          "No automatic action. Review notes. Change manually to PENDING if conditions change."],
    ["PAUSED_SEASONAL",  "Right product, wrong season. Flagged for a future re-eval date.",                     "Auto — set by researcher based on seasonal scoring.",                     "Agent re-evaluates on RE-EVAL DATE (col S). May promote to PENDING in-season."],
]


def _ensure_legend_tab(service, spreadsheet_id):
    meta = _get_sheet_meta(service, spreadsheet_id)
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if "Legend" not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": "Legend", "index": 2}}}]},
        ).execute()
        logger.info("  Created Legend tab.")
        meta = _get_sheet_meta(service, spreadsheet_id)

    legend_id = _get_tab_id(meta, "Legend")

    # ── Content rows ─────────────────────────────────────────────────────────────
    # Each section starts with a DIVIDER row (all-caps label in col A, cols B-D blank).
    # Divider rows get dark navy background + white text in the formatting pass below.
    # We track which row indices are dividers so we can style them.

    rows = []
    divider_rows  = []   # 0-based row indices that get section-divider styling
    status_rows   = []   # (row_idx, status_name) for color coding
    col_hdr_rows  = []   # 0-based row indices for column-header style (STATUS | MEANING …)

    def _divider(label):
        divider_rows.append(len(rows))
        rows.append([label, "", "", ""])

    def _blank():
        rows.append(["", "", "", ""])

    def _row(*cells):
        rows.append(list(cells) + [""] * (4 - len(cells)))

    # ── Row 0: Main title ────────────────────────────────────────────────────────
    divider_rows.append(0)
    rows.append(["WAT Agent — Reference Guide", "", "", ""])

    _blank()

    # ── STATUS DEFINITIONS ───────────────────────────────────────────────────────
    _divider("STATUS DEFINITIONS")
    col_hdr_rows.append(len(rows))
    _row("STATUS", "MEANING", "WHEN IT'S SET", "NEXT ACTION")
    for status_row in STATUS_LEGEND[1:]:   # skip the original header
        status_name = status_row[0]
        status_rows.append((len(rows), status_name))
        rows.append(list(status_row))

    _blank()

    # ── TIER & COMP SCORE ────────────────────────────────────────────────────────
    _divider("TIER SCORE  (col B)")
    _row("Tier 1  ≥ 7.0", "Strong opportunity — PENDING status. Review and approve to list.", "", "Green cell")
    _row("Tier 2  4.0–6.9", "Promising but not ready — WATCH status. Re-scored weekly automatically.", "", "Yellow cell")
    _row("Tier 3  < 4.0", "Not viable now — PAUSED_DEMAND. Re-eval in 30 days.", "", "Red cell")

    _blank()

    _divider("COMP SCORE  (col N)  —  active listings ÷ 90-day sold count")
    _row("Low  (green)",  "< 2× — fewer sellers than buyers. Strong demand window.", "", "< 2×")
    _row("Med  (yellow)", "2× to 10× — normal competitive market. Pricing and photos matter.", "", "2–10×")
    _row("High (orange)", "≥ 10× — saturated. Avoid unless margin is exceptional.", "", "> 10×")

    _blank()

    # ── COLUMN REFERENCE ─────────────────────────────────────────────────────────
    _divider("COLUMN QUICK REFERENCE")
    _row("Col A", "STATUS — current pipeline stage. Use the dropdown to change.", "", "")
    _row("Col B", "TIER SCORE — 0-10 composite. Green ≥7, Yellow 4-7, Red <4.", "", "")
    _row("Col G", "COST $ — what you pay at Costco.", "", "")
    _row("Col H", "eBay PRICE — your listing price. Agent sets once; edit freely.", "", "")
    _row("Col I", "NET PROFIT — formula: eBay Price − Cost − Fees − Shipping.", "", "")
    _row("Col J", "MARGIN — formula: Net Profit / eBay Price. Target ≥ 10%.", "", "")
    _row("Col N", "COMP SCORE — active listings / 90d sold. Low = less competition.", "", "")
    _row("Col Q", "eBay LISTING — paste your live eBay URL here → triggers ACTIVE monitoring.", "", "")
    _row("Col T", "TIER SUMMARY — short one-liner: [T2 | Score 8.2 | Sugg: $899 | margin 18%].", "", "")
    _row("Col X", "SALE — 🔥 -$150 ends 5/31 badge when product is on sale at Costco. Blank otherwise.", "", "")
    _row("Col Y", "SHIP COST — ✓ FREE (teal) when free shipping; $12.99 ship (amber) when paid. Blank if unknown.", "", "")
    _row("Col Z", "TOTAL COST — formula: Costco cost + shipping. All-in delivered price. Never overwrite.", "", "")
    _row("Col V", "SUGG. PRICE — agent's one-time recommended price. Do not overwrite.", "", "")
    _row("Col W", "PURCH. LIMIT — Costco daily buy limit. Precious metals: 2/day.", "", "")

    _blank()

    # ── 5-STEP WORKFLOW ──────────────────────────────────────────────────────────
    _divider("5-STEP WORKFLOW")
    _row("1  Discover",  "Agent scrapes Costco → adds new products as PENDING. Runs 7 AM daily (GitHub).")
    _row("2  Research",  "Agent scores PENDING → Tier 1 stays PENDING, Tier 2 becomes WATCH. Runs 10 AM.")
    _row("3  Approve",   "YOU change col A from PENDING → APPROVED. Agent generates listing copy + verifies stock.")
    _row("4  List",      "Agent sets APPROVED → READY. Export CSV (VS Code task) → upload to eBay Seller Hub.")
    _row("5  Monitor",   "Paste eBay URL in col Q → auto-set to ACTIVE. Active Monitor checks stock/price 3×/day.")

    _blank()

    # ── VS CODE TASKS ─────────────────────────────────────────────────────────────
    _divider("VS CODE TASKS   Ctrl+Shift+P → 'Tasks: Run Task'")
    _row("WAT: Check Status",           "Quick snapshot — last runs, product counts, spot prices, next scheduled run.")
    _row("WAT: Run Discovery",          "Scrape Costco for new products → PENDING. Also runs automatically at 7 AM.")
    _row("WAT: Run Research",           "Score all PENDING rows (eBay comps + Claude). Also runs at 10 AM.")
    _row("WAT: Research — Re-score Only", "Same as Research but skips discovery. ~2 min. Use after manually adding rows.")
    _row("WAT: Run Daily Sweep",        "APPROVED→READY + PAUSED stock/margin recovery. Also runs at 9 AM.")
    _row("WAT: Run Rotation Digest",    "Weekly: score all WATCH products, flag underperformers. Auto-runs Fridays.")
    _row("WAT: Run Active Monitor",     "LOCAL ONLY — checks stock/price for ACTIVE listings. Needs Chrome open.")
    _row("WAT: Setup Sheet",            "Re-apply dashboard formatting. Safe to re-run anytime sheet looks wrong.")
    _row("WAT: Export eBay CSV",        "Generate upload CSV for READY rows → upload at eBay Seller Hub.")

    # ── Write values ─────────────────────────────────────────────────────────────
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="'Legend'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    # ── Formatting ───────────────────────────────────────────────────────────────
    total_rows = len(rows)
    requests = [
        # Freeze row 1 (title)
        {"updateSheetProperties": {
            "properties": {"sheetId": legend_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        # Base style for all content rows: wrap text, font 10, top-align
        {"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": 1, "endRowIndex": total_rows + 5,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "wrapStrategy": "WRAP",
                "verticalAlignment": "TOP",
                "textFormat": {"fontSize": 10},
                "padding": {"top": 5, "bottom": 5, "left": 8, "right": 4},
            }},
            "fields": "userEnteredFormat",
        }},
        # Column widths: label | description | detail | tag
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 140}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 520}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 280}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
            "properties": {"pixelSize": 80}, "fields": "pixelSize",
        }},
    ]

    # Section dividers: dark navy bg + white bold text spanning all 4 cols
    for row_idx in divider_rows:
        is_title = (row_idx == 0)
        requests.append({"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": TITLE_BG if is_title else HDR_BG,
                "textFormat": {
                    "foregroundColor": HDR_FG, "bold": True,
                    "fontSize": 13 if is_title else 10,
                },
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 6, "bottom": 6, "left": 10, "right": 4},
            }},
            "fields": "userEnteredFormat",
        }})
        # Row height: taller for dividers
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "ROWS",
                       "startIndex": row_idx, "endIndex": row_idx + 1},
            "properties": {"pixelSize": 32 if is_title else 26},
            "fields": "pixelSize",
        }})

    # Column header rows (STATUS | MEANING | …): medium navy, bold
    for row_idx in col_hdr_rows:
        requests.append({"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": STATS_BG,
                "textFormat": {"foregroundColor": HDR_FG, "bold": True, "fontSize": 9},
                "horizontalAlignment": "LEFT",
            }},
            "fields": "userEnteredFormat",
        }})

    # Status rows: color-code the label cell (col A) to match Product Tracker
    for row_idx, status_name in status_rows:
        color = STATUS_COLORS.get(status_name, _rgb("FFFFFF"))
        requests.append({"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                       "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": color,
                "textFormat": {"bold": True, "fontSize": 10},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat",
        }})

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
    logger.info("  Legend tab refreshed.")


def _write_header_text(service, spreadsheet_id, sheet_name, data_start_row):
    header_row = data_start_row - 1
    stats_row = [""] * VISIBLE_COLS
    stats_row[0]  = "PIPELINE  P:—  T1:—  ACT:—  RDY:—  URG:—  TOT:—"
    stats_row[8]  = "OPPORTUNITIES  🔥— ON SALE  📦— FREE SHIP"
    stats_row[14] = "FOCUS  Top: —  Score: —"
    stats_row[21] = "Last run: —"
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": [
            {"range": f"'{sheet_name}'!A1",
             "values": [["Costco → eBay Reselling Dashboard  |  JA_Liquidations"]]},
            {"range": f"'{sheet_name}'!A2",
             "values": [stats_row]},
            {"range": f"'{sheet_name}'!A{header_row}",
             "values": [HEADER_LABELS]},
        ]},
    ).execute()


def update_stats_row(service, sheet_name, stats: dict):
    """
    Update the stats banner (row 2) with live counts after each agent run.
    Writes a rich multi-section layout across visible cols A–Y.
    """
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")

    lr   = stats.get("last_run", "—")
    tot  = stats.get("total", "—")
    t1   = stats.get("tier1", "—")
    pend = stats.get("pending", "—")
    urg  = stats.get("urgent", "—")
    sale = stats.get("on_sale", "—")
    ship = stats.get("free_ship", "—")
    act  = stats.get("active", "—")
    rdy  = stats.get("ready", "—")
    top  = stats.get("top_pending_title", "—")
    top_score = stats.get("top_pending_score", "—")

    # Section text for each cell region (written to specific columns)
    pipeline_txt = f"PIPELINE  P:{pend}  T1:{t1}  ACT:{act}  RDY:{rdy}  URG:{urg}  TOT:{tot}"
    opps_txt     = f"OPPORTUNITIES  🔥{sale} ON SALE  📦{ship} FREE SHIP"
    focus_txt    = f"FOCUS  Top: {str(top)[:30]}  Score:{top_score}"
    run_txt      = f"Last run: {lr}"

    values = [[""] * VISIBLE_COLS]
    values[0][0]  = pipeline_txt   # A2
    values[0][8]  = opps_txt       # I2
    values[0][14] = focus_txt      # O2
    values[0][21] = run_txt        # V2

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A2",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def refresh_summary_tab(service, sheet_name, all_data=None):
    """
    Builds or refreshes the Summary tab with live pipeline counts, category stats,
    and focus recommendations.

    all_data: list of sheet rows (A–AZ) from read_sheet. If None, reads from sheet.
    Safe to re-run anytime — overwrites previous content.
    """
    import os
    from collections import defaultdict

    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    meta           = _get_sheet_meta(service, spreadsheet_id)
    existing       = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    # Create Summary tab if missing (index 0 = leftmost)
    if "Summary" not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": "Summary", "index": 0}}}]},
        ).execute()
        logger.info("  Created Summary tab.")
        meta     = _get_sheet_meta(service, spreadsheet_id)
        existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    sum_id = existing["Summary"]

    # ── Compute stats from all_data ───────────────────────────────────────────
    pipeline = defaultdict(int)
    category_stats = defaultdict(lambda: {"count": 0, "score_sum": 0.0, "score_count": 0,
                                           "margin_sum": 0.0, "margin_count": 0, "t1": 0})
    top_pending = []   # (score, title, costco_url) for focus recommendation
    sale_count  = 0
    ship_count  = 0

    def _safe(lst, i):
        return str(lst[i]).strip() if lst and i < len(lst) else ""

    for row in (all_data or []):
        if not row or not row[0]:
            continue
        status   = _safe(row, 0)   # A
        score_s  = _safe(row, 1)   # B
        title    = _safe(row, 2)   # C
        category = _safe(row, 3)   # D
        ebay_s   = _safe(row, 7)   # H
        cost_s   = _safe(row, 6)   # G
        url      = _safe(row, 17)  # R
        sale_val = _safe(row, 23)  # X — dedicated SALE col
        ship_val = _safe(row, 24)  # Y — ship cost badge

        if not status:
            continue

        pipeline[status] += 1

        try:
            score = float(score_s)
            cat   = category_stats[category or "Unknown"]
            cat["count"]       += 1
            cat["score_sum"]   += score
            cat["score_count"] += 1
            if score >= 7.0:
                cat["t1"] += 1
        except (ValueError, TypeError):
            score = None

        try:
            ep = float(str(ebay_s).replace("$", "").replace(",", ""))
            cp = float(str(cost_s).replace("$", "").replace(",", ""))
            if ep > 0:
                margin = (ep - cp - ep * 0.1325) / ep
                cat = category_stats[category or "Unknown"]
                cat["margin_sum"]   += margin
                cat["margin_count"] += 1
        except (ValueError, TypeError):
            pass

        if sale_val:
            sale_count += 1
        if "FREE" in ship_val:
            ship_count += 1

        if status == "PENDING" and score is not None:
            top_pending.append((score, title, url))

    top_pending.sort(reverse=True)

    # ── Build row content ─────────────────────────────────────────────────────
    rows = []

    def _hdr(label):
        rows.append([label, "", "", "", ""])

    def _blank():
        rows.append(["", "", "", "", ""])

    def _row(*cells):
        r = list(cells)
        r += [""] * (5 - len(r))
        rows.append(r)

    # Title
    _hdr("WAT Agent — Live Summary Dashboard")
    _blank()

    # Pipeline counts
    _hdr("PIPELINE STATUS")
    _row("Status", "Count", "", "", "")
    status_order = ["PENDING", "WATCH", "APPROVED", "READY", "ACTIVE",
                    "PAUSED_OOS", "PAUSED_MARGIN", "PAUSED_DEMAND", "PAUSED_SEASONAL"]
    total = sum(pipeline.values())
    for s in status_order:
        if pipeline[s] > 0:
            _row(s, pipeline[s], "", "", "")
    _row("TOTAL", total, "", "", "")
    _blank()

    # Highlights
    _hdr("HIGHLIGHTS")
    _row("Products currently ON SALE at Costco", sale_count)
    _row("Products with FREE SHIPPING from Costco", ship_count)
    _row("Active (live on eBay)", pipeline.get("ACTIVE", 0))
    _row("Ready to list (copy done)", pipeline.get("READY", 0))
    _blank()

    # Category breakdown
    _hdr("CATEGORY BREAKDOWN")
    _row("Category", "Products", "Avg Score", "T1 Count", "Avg Margin")
    best_cat, best_margin = "", -999.0
    for cat_name, d in sorted(category_stats.items()):
        avg_score  = round(d["score_sum"] / d["score_count"], 2) if d["score_count"] else "—"
        avg_margin = (
            f"{d['margin_sum'] / d['margin_count'] * 100:.1f}%"
            if d["margin_count"] else "—"
        )
        _row(cat_name, d["count"], avg_score, d["t1"], avg_margin)
        if d["margin_count"] and d["margin_sum"] / d["margin_count"] > best_margin:
            best_margin = d["margin_sum"] / d["margin_count"]
            best_cat    = cat_name
    _blank()

    # Focus recommendation
    _hdr("WHERE TO FOCUS")
    if best_cat:
        _row("Highest-margin category", best_cat, f"{best_margin * 100:.1f}% avg margin")
    if top_pending:
        top_score, top_title, top_url = top_pending[0]
        _row("Top PENDING to approve", top_title[:60], f"Score: {top_score}", top_url)
    if pipeline.get("READY", 0):
        _row("Action needed", f"{pipeline['READY']} product(s) READY — export CSV and list on eBay")
    if pipeline.get("PAUSED_OOS", 0):
        _row("Monitor", f"{pipeline['PAUSED_OOS']} OOS — check if restocked")
    _blank()

    _row(f"Last refreshed: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # ── Write content ─────────────────────────────────────────────────────────
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="'Summary'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    # ── Format ────────────────────────────────────────────────────────────────
    total_rows = len(rows)
    hdr_rows   = [i for i, r in enumerate(rows) if r and r[0] and not r[1] and r[0] != ""]

    requests = [
        # Freeze row 1
        {"updateSheetProperties": {
            "properties": {"sheetId": sum_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        # Base style
        {"repeatCell": {
            "range": {"sheetId": sum_id, "startRowIndex": 0, "endRowIndex": total_rows + 5,
                       "startColumnIndex": 0, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {
                "textFormat": {"fontSize": 10},
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 4, "bottom": 4, "left": 8, "right": 4},
            }},
            "fields": "userEnteredFormat",
        }},
        # Column widths
        {"updateDimensionProperties": {
            "range": {"sheetId": sum_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 220}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sum_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 100}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sum_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 120}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sum_id, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
            "properties": {"pixelSize": 80}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sum_id, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 5},
            "properties": {"pixelSize": 350}, "fields": "pixelSize",
        }},
    ]

    # Section header rows — dark navy
    for i, r in enumerate(rows):
        label = r[0] if r else ""
        is_title = (i == 0)
        if not label or r[1]:  # skip blank or data rows
            continue
        if any(label == s for s in status_order + ["Status", "Category", "Products currently ON SALE at Costco",
                                                     "Action needed", "Monitor", "Top PENDING to approve",
                                                     "Highest-margin category"]):
            continue  # these are data rows, not section headers
        requests.append({"repeatCell": {
            "range": {"sheetId": sum_id, "startRowIndex": i, "endRowIndex": i + 1,
                       "startColumnIndex": 0, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {
                "backgroundColor": TITLE_BG if is_title else HDR_BG,
                "textFormat": {
                    "foregroundColor": HDR_FG, "bold": True,
                    "fontSize": 13 if is_title else 10,
                },
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat",
        }})
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sum_id, "dimension": "ROWS",
                       "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": 34 if is_title else 26},
            "fields": "pixelSize",
        }})

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
    logger.info("  Summary tab refreshed.")


def populate_images_tab(service, sheet_name, all_data, col_map):
    """
    Refreshes the Images tab with ACTIVE and APPROVED products + their image URLs.
    Called at the end of each scheduler run.
    """
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    rows = [["PRODUCT TITLE", "STATUS", "IMAGE 1", "IMAGE 2", "IMAGE 3", "IMAGE 4", "IMAGE 5"]]

    status_col = ord(col_map.get("status", "A")) - ord("A")        # 0
    title_col  = ord(col_map.get("title", "C")) - ord("C") + 2     # 2
    img_col    = col_map.get("image_urls", "AM")

    # Convert AM-style letter to 0-based index
    def letter_to_idx(letter):
        result = 0
        for ch in letter:
            result = result * 26 + (ord(ch.upper()) - ord("A") + 1)
        return result - 1

    img_idx    = letter_to_idx(img_col)   # AM = 38

    def safe(lst, i):
        return lst[i] if i < len(lst) else ""

    for row in all_data:
        if not row:
            continue
        status = safe(row, 0)   # col A
        title  = safe(row, 2)   # col C
        if status not in ("ACTIVE", "APPROVED", "READY"):
            continue
        raw_imgs = safe(row, img_idx)
        if not raw_imgs:
            continue
        img_urls = [u.strip() for u in raw_imgs.split(",") if u.strip()]
        img_urls += [""] * (5 - len(img_urls))
        rows.append([title, status] + img_urls[:5])

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="'Images'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()
    logger.info(f"  Images tab updated — {len(rows) - 1} products.")
