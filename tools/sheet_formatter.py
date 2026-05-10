"""
Tool: sheet_formatter
Builds the Product Tracker dashboard and supporting tabs.

Layout: STATUS and TIER SCORE are columns A and B — always visible.
Columns A–D are frozen so they stay in view while scrolling right.
Columns T–AM are hidden (SEO copy, formulas, ad copy, image URLs).

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
    "RESEARCH NOTES",  # T  19
    "UNITS SOLD",      # U  20 — manually entered or future eBay API
    # V–AR hidden
]

COLUMN_WIDTHS = {
    0:  95,    # A: status
    1:  60,    # B: tier score
    2:  280,   # C: title
    3:  90,    # D: category
    4:  75,    # E: platform
    5:  100,   # F: stock
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
    19: 380,   # T: research notes
    20: 70,    # U: units sold
}

VISIBLE_COLS  = 21    # A–U
TOTAL_COLS    = 44    # A–AR
HIDDEN_START  = 21    # V onwards (index 21 = col V)
FROZEN_COLS   = 4     # A–D always visible


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

    # 11. Hide columns T–AM (index 19–38)
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
    # Notes column (T = index 19): wrap text
    requests.append({
        "repeatCell": {
            "range": _cell_range(tab_id, data_row_idx, 1000, 19, 20),
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "textFormat": {"fontSize": 8}}},
            "fields": "userEnteredFormat(wrapStrategy,textFormat)",
        }
    })

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
    # Write content
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="'Legend'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [
            ["Status Definitions — Product Tracker"],
            [""],
        ] + STATUS_LEGEND + [
            [""],
            ["TIER SCORE (col B)"],
            ["Tier 1", "Score >= 7.0 — strong buy signal across demand, margin, competition.", "", ""],
            ["Tier 2", "Score 4.0–6.9 — borderline. Goes to WATCH; agent rechecks weekly.", "", ""],
            ["Tier 3", "Score < 4.0 — skipped. Not added to tracker (or marked PAUSED).", "", ""],
            [""],
            ["COMP SCORE (col N) — active÷sold ratio. Lower = more headroom."],
            ["Low",  "< 2× — fewer active listings than recent sales. Strong demand vs supply.", "", ""],
            ["Med",  "2× to 10× — typical competitive market. Pricing matters.", "", ""],
            ["High", ">= 10× — saturated. Many sellers, slow turnover. Avoid unless margin is exceptional.", "", ""],
        ]},
    ).execute()

    # Format
    requests = [
        # Title
        {"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": 0, "endRowIndex": 1,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": TITLE_BG,
                "textFormat": {"foregroundColor": HDR_FG, "bold": True, "fontSize": 13},
            }}, "fields": "userEnteredFormat",
        }},
        # Header row of legend table (row 3, idx 2)
        {"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": 2, "endRowIndex": 3,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HDR_BG,
                "textFormat": {"foregroundColor": HDR_FG, "bold": True, "fontSize": 10},
                "horizontalAlignment": "LEFT",
            }}, "fields": "userEnteredFormat",
        }},
        # Wrap text for readability
        {"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": 3, "endRowIndex": 30,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "wrapStrategy": "WRAP",
                "verticalAlignment": "TOP",
                "textFormat": {"fontSize": 10},
            }}, "fields": "userEnteredFormat",
        }},
        # Column widths
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 110}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 380}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 320}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
            "properties": {"pixelSize": 380}, "fields": "pixelSize",
        }},
        # Row colors for status rows (rows 4-9 = idx 3-8)
        {"updateSheetProperties": {
            "properties": {"sheetId": legend_id, "gridProperties": {"frozenRowCount": 3}},
            "fields": "gridProperties.frozenRowCount",
        }},
    ]
    # Color-code each status row to match Product Tracker
    for i, status in enumerate([
        "PENDING", "APPROVED", "READY", "ACTIVE", "WATCH",
        "PAUSED_OOS", "PAUSED_MARGIN", "PAUSED_DEMAND", "PAUSED_SEASONAL",
    ]):
        row_idx = 3 + i  # rows 4-9 (0-indexed 3-8)
        requests.append({"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                       "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": STATUS_COLORS[status],
                "textFormat": {"bold": True, "fontSize": 10},
                "horizontalAlignment": "CENTER",
            }}, "fields": "userEnteredFormat",
        }})
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
    logger.info("  Legend tab refreshed.")


def _write_header_text(service, spreadsheet_id, sheet_name, data_start_row):
    header_row = data_start_row - 1
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": [
            {"range": f"'{sheet_name}'!A1",
             "values": [["Costco → eBay Reselling Dashboard"]]},
            {"range": f"'{sheet_name}'!A2",
             "values": [["Last run: — | Tracked: — | Tier 1 ready: — | Pending: — | Urgent: —"]]},
            {"range": f"'{sheet_name}'!A{header_row}",
             "values": [HEADER_LABELS]},
        ]},
    ).execute()


def update_stats_row(service, sheet_name, stats: dict):
    """Update the stats banner (row 2) with live counts after each agent run."""
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    text = (
        f"Last run: {stats.get('last_run', '—')}  |  "
        f"Tracked: {stats.get('total', '—')}  |  "
        f"Tier 1 ready: {stats.get('tier1', '—')}  |  "
        f"Pending review: {stats.get('pending', '—')}  |  "
        f"Urgent: {stats.get('urgent', '—')}"
    )
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A2",
        valueInputOption="USER_ENTERED",
        body={"values": [[text]]},
    ).execute()


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
