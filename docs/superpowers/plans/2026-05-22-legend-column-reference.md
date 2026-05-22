# Legend Column Reference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document all 48 columns (A–AV) in the Legend tab and write header labels for hidden columns AA–AV to Product Tracker row 3.

**Architecture:** Two changes, both in `tools/sheet_formatter.py`. First: extend `HEADER_LABELS` from 26 to 48 entries — `_write_header_text` already writes this list to row 3, so hidden col labels appear automatically. Second: replace the 12-entry "COLUMN QUICK REFERENCE" section in `_ensure_legend_tab` with two full sections — VISIBLE COLUMNS (A–Z, 26 rows) and HIDDEN COLUMNS (AA–AV, 22 rows) — with distinct divider styling.

**Tech Stack:** Python 3, Google Sheets API (via `tools/sheet_formatter.py`), pytest.

---

### Task 1: Extend `HEADER_LABELS` to 48 entries

**Files:**
- Modify: `tools/sheet_formatter.py`
- Test: `tests/test_menu.py` (add — small enough to sit alongside other formatter checks)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_menu.py`:

```python
# ── HEADER_LABELS completeness ────────────────────────────────────────────────

def test_header_labels_covers_all_48_columns():
    from tools.sheet_formatter import HEADER_LABELS, TOTAL_COLS
    assert len(HEADER_LABELS) == TOTAL_COLS, (
        f"Expected {TOTAL_COLS} labels, got {len(HEADER_LABELS)}"
    )


def test_header_labels_visible_cols_unchanged():
    """First 26 labels (A–Z) must stay exactly as they are."""
    from tools.sheet_formatter import HEADER_LABELS
    expected_visible = [
        "STATUS", "TIER", "PRODUCT TITLE", "CATEGORY", "PLATFORM", "STOCK",
        "COST $", "eBay PRICE", "NET PROFIT", "MARGIN", "SOLD 90d", "AVG eBay $",
        "ACTIVE", "COMP SCORE", "LAST CHECKED", "PRICE CHG", "eBay LISTING",
        "COSTCO URL", "RE-EVAL DATE", "TIER SUMMARY", "UNITS SOLD", "SUGG. PRICE",
        "PURCH. LIMIT", "SALE", "SHIP COST", "TOTAL COST",
    ]
    assert HEADER_LABELS[:26] == expected_visible


def test_header_labels_hidden_cols_present():
    """Spot-check key hidden col labels by position."""
    from tools.sheet_formatter import HEADER_LABELS
    # AA=26, AB=27, AD=29, AT=45, AU=46, AV=47
    assert HEADER_LABELS[26] == "SKU"
    assert HEADER_LABELS[27] == "FEE RATE"
    assert HEADER_LABELS[29] == "SHIP COST $"
    assert HEADER_LABELS[45] == "IMAGE URLS"
    assert HEADER_LABELS[46] == "PERF SCORE"
    assert HEADER_LABELS[47] == "FULL NOTES"
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_menu.py::test_header_labels_covers_all_48_columns tests/test_menu.py::test_header_labels_visible_cols_unchanged tests/test_menu.py::test_header_labels_hidden_cols_present -v
```

Expected: FAIL — `len(HEADER_LABELS)` is 26, not 48.

- [ ] **Step 3: Replace `HEADER_LABELS` in sheet_formatter.py**

In `tools/sheet_formatter.py`, find the `HEADER_LABELS` list (line ~55). Replace it entirely:

```python
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
    "ACTIVE",          # M  12
    "COMP SCORE",      # N  13
    "LAST CHECKED",    # O  14
    "PRICE CHG",       # P  15
    "eBay LISTING",    # Q  16
    "COSTCO URL",      # R  17
    "RE-EVAL DATE",    # S  18
    "TIER SUMMARY",    # T  19
    "UNITS SOLD",      # U  20
    "SUGG. PRICE",     # V  21
    "PURCH. LIMIT",    # W  22
    "SALE",            # X  23
    "SHIP COST",       # Y  24
    "TOTAL COST",      # Z  25
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_menu.py::test_header_labels_covers_all_48_columns tests/test_menu.py::test_header_labels_visible_cols_unchanged tests/test_menu.py::test_header_labels_hidden_cols_present -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add tools/sheet_formatter.py tests/test_menu.py
git commit -m "feat: extend HEADER_LABELS to all 48 columns (AA-AV hidden col headers)"
```

---

### Task 2: Replace "COLUMN QUICK REFERENCE" with two-section column reference in `_ensure_legend_tab`

**Files:**
- Modify: `tools/sheet_formatter.py`
- Test: `tests/test_menu.py` (continue adding — test the data layer, not the API calls)

The Legend tab content is built by assembling a `rows` list, then writing it to Sheets. To keep the logic testable without hitting the API, extract the row-building into a helper `_build_legend_rows()` that returns `(rows, divider_rows, status_rows, col_hdr_rows)`. `_ensure_legend_tab` calls it and handles the API writes.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_menu.py`:

```python
# ── Legend row content ────────────────────────────────────────────────────────

def test_legend_rows_contain_visible_columns_section():
    from tools.sheet_formatter import _build_legend_rows
    rows, _, _, _ = _build_legend_rows()
    flat = [r[0] for r in rows if r]
    assert "VISIBLE COLUMNS  (A–Z)" in flat


def test_legend_rows_contain_hidden_columns_section():
    from tools.sheet_formatter import _build_legend_rows
    rows, _, _, _ = _build_legend_rows()
    flat = [r[0] for r in rows if r]
    assert "HIDDEN COLUMNS  (AA–AV)" in flat


def test_legend_rows_include_all_26_visible_col_letters():
    from tools.sheet_formatter import _build_legend_rows
    rows, _, _, _ = _build_legend_rows()
    col_labels = {r[0] for r in rows if r and len(r[0]) <= 2 and r[0].isalpha() and r[0].isupper()}
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        assert letter in col_labels, f"Visible col {letter} missing from Legend rows"


def test_legend_rows_include_all_22_hidden_col_labels():
    from tools.sheet_formatter import _build_legend_rows
    rows, _, _, _ = _build_legend_rows()
    col_labels = {r[0] for r in rows if r}
    for col in ["AA", "AB", "AC", "AD", "AE", "AF", "AG", "AH",
                "AI", "AJ", "AK", "AL", "AM", "AN", "AO",
                "AP", "AQ", "AR", "AS", "AT", "AU", "AV"]:
        assert col in col_labels, f"Hidden col {col} missing from Legend rows"


def test_legend_rows_no_old_column_quick_reference():
    from tools.sheet_formatter import _build_legend_rows
    rows, _, _, _ = _build_legend_rows()
    flat = [r[0] for r in rows if r]
    assert "COLUMN QUICK REFERENCE" not in flat
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_menu.py::test_legend_rows_contain_visible_columns_section tests/test_menu.py::test_legend_rows_contain_hidden_columns_section tests/test_menu.py::test_legend_rows_include_all_26_visible_col_letters tests/test_menu.py::test_legend_rows_include_all_22_hidden_col_labels tests/test_menu.py::test_legend_rows_no_old_column_quick_reference -v
```

Expected: FAIL — `_build_legend_rows` does not exist.

- [ ] **Step 3: Extract `_build_legend_rows()` from `_ensure_legend_tab`**

In `tools/sheet_formatter.py`, find `_ensure_legend_tab` (line ~680). Extract the row-building logic into a new function placed just before `_ensure_legend_tab`:

```python
def _build_legend_rows():
    """
    Build the Legend tab content rows.
    Returns (rows, divider_rows, status_rows, col_hdr_rows).
    All indices are 0-based row positions within the returned rows list.
    """
    rows = []
    divider_rows = []
    status_rows  = []
    col_hdr_rows = []

    def _divider(label):
        divider_rows.append(len(rows))
        rows.append([label, "", "", ""])

    def _blank():
        rows.append(["", "", "", ""])

    def _row(*cells):
        rows.append(list(cells) + [""] * (4 - len(cells)))

    # ── Row 0: Main title ────────────────────────────────────────────────────
    divider_rows.append(0)
    rows.append(["WAT Agent — Reference Guide", "", "", ""])

    _blank()

    # ── STATUS DEFINITIONS ───────────────────────────────────────────────────
    _divider("STATUS DEFINITIONS")
    col_hdr_rows.append(len(rows))
    _row("STATUS", "MEANING", "WHEN IT'S SET", "NEXT ACTION")
    for status_row in STATUS_LEGEND[1:]:
        status_name = status_row[0]
        status_rows.append((len(rows), status_name))
        rows.append(list(status_row))

    _blank()

    # ── TIER & COMP SCORE ────────────────────────────────────────────────────
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

    # ── VISIBLE COLUMNS (A–Z) ────────────────────────────────────────────────
    _divider("VISIBLE COLUMNS  (A–Z)")
    _row("", "Formula columns are maintained by the sheet — do not overwrite.", "", "")
    col_hdr_rows.append(len(rows))
    _row("COL", "HEADER", "WHAT IT STORES", "NOTES")

    visible_cols = [
        ("A", "STATUS",        "Current pipeline stage. Use the dropdown.",                              ""),
        ("B", "TIER",          "0–10 composite score. Green ≥7, Yellow 4–7, Red <4.",                   ""),
        ("C", "PRODUCT TITLE", "Product name from Costco.",                                              ""),
        ("D", "CATEGORY",      "Category (Precious Metals, Jewelry, Watches, etc.).",                   ""),
        ("E", "PLATFORM",      "eBay / Site / Both — set when listed.",                                  ""),
        ("F", "STOCK",         "In Stock / Available (2/day limit) / OUT OF STOCK.",                     ""),
        ("G", "COST $",        "What you pay at Costco.",                                                ""),
        ("H", "eBay PRICE",    "Your eBay listing price. Agent sets once; edit freely.",                 ""),
        ("I", "NET PROFIT",    "eBay Price − Cost − Fees − Shipping.",                                   "formula"),
        ("J", "MARGIN",        "Net Profit / eBay Price. Target ≥ 10%.",                                 "formula"),
        ("K", "SOLD 90d",      "eBay sold listings in the last 90 days.",                                ""),
        ("L", "AVG eBay $",    "Average eBay sold price across comps.",                                  ""),
        ("M", "ACTIVE",        "Active competing eBay listings count.",                                  ""),
        ("N", "COMP SCORE",    "Active listings ÷ 90d sold. Low < 2×, Med < 10×, High ≥ 10×.",         "formula"),
        ("O", "LAST CHECKED",  "Timestamp of last agent run for this product.",                          ""),
        ("P", "PRICE CHG",     "Flag: Costco price moved since last check.",                             ""),
        ("Q", "eBay LISTING",  "Paste live eBay URL here → triggers ACTIVE monitoring.",                 ""),
        ("R", "COSTCO URL",    "Source product link on Costco.com.",                                     ""),
        ("S", "RE-EVAL DATE",  "PAUSED items: date agent will re-research.",                             ""),
        ("T", "TIER SUMMARY",  "Short one-liner: [T2 | Score 8.2 | Sugg: $899 | margin 18%].",          ""),
        ("U", "UNITS SOLD",    "Units sold (manually entered or future eBay API).",                      ""),
        ("V", "SUGG. PRICE",   "Agent's one-time recommended eBay price. Do not overwrite.",             ""),
        ("W", "PURCH. LIMIT",  "Costco daily buy limit (precious metals: 2/day, else blank).",           ""),
        ("X", "SALE",          "🔥 -$150 ends 5/31 badge when product is on sale at Costco.",            ""),
        ("Y", "SHIP COST",     "✓ FREE (teal) or $12.99 ship (amber). Blank if unknown.",                ""),
        ("Z", "TOTAL COST",    "Costco cost + shipping. All-in delivered price.",                        "formula"),
    ]
    for col, header, desc, notes in visible_cols:
        _row(col, header, desc, notes)

    _blank()

    # ── HIDDEN COLUMNS (AA–AV) ───────────────────────────────────────────────
    _divider("HIDDEN COLUMNS  (AA–AV)")
    _row("", "Agent-managed. Do not edit directly.", "", "")
    col_hdr_rows.append(len(rows))
    _row("COL", "KEY", "WHAT IT STORES", "")

    hidden_cols = [
        ("AA", "SKU",          "Costco product SKU."),
        ("AB", "FEE RATE",     "eBay fee rate applied to this product (default 13.25%)."),
        ("AC", "eBay FEES",    "formula: eBay Price × Fee Rate."),
        ("AD", "SHIP COST $",  "Shipping cost in dollars (raw value behind the Y badge)."),
        ("AE", "FULFILLMENT",  "Fulfillment cost if applicable."),
        ("AF", "TAX EST.",     "formula: Costco Cost × 8.25%."),
        ("AG", "SITE PROFIT",  "formula: eBay Price × 0.90 − Cost."),
        ("AH", "AD BUDGET",    "formula: Net Profit × 15%."),
        ("AI", "SEO TITLE",    "Optimized listing title for eBay / site."),
        ("AJ", "BULLETS",      "Bullet points for eBay listing description."),
        ("AK", "DESCRIPTION",  "Full listing description."),
        ("AL", "REDIRECT MSG", "Message shown if product is no longer available."),
        ("AM", "META DESC",    "SEO meta description."),
        ("AN", "KEYWORDS",     "Search keywords for eBay / Google."),
        ("AO", "ALT TEXT",     "Image alt text for accessibility / SEO."),
        ("AP", "GOOGLE HL",    "Google Shopping headline."),
        ("AQ", "GOOGLE DESC",  "Google Shopping description."),
        ("AR", "META TEXT",    "Additional meta text."),
        ("AS", "META HL",      "Meta headline."),
        ("AT", "IMAGE URLS",   "Comma-separated Costco image URLs."),
        ("AU", "PERF SCORE",   "Composite performance score (0–10) written by rotation engine."),
        ("AV", "FULL NOTES",   "Full research narrative, eBay comps, community signals."),
    ]
    for col, key, desc in hidden_cols:
        _row(col, key, desc, "")

    _blank()

    # ── 5-STEP WORKFLOW ──────────────────────────────────────────────────────
    _divider("5-STEP WORKFLOW")
    _row("1  Discover",  "Agent scrapes Costco → adds new products as PENDING. Runs 7 AM daily (GitHub).")
    _row("2  Research",  "Agent scores PENDING → Tier 1 stays PENDING, Tier 2 becomes WATCH. Runs 10 AM.")
    _row("3  Approve",   "YOU change col A from PENDING → APPROVED. Agent generates listing copy + verifies stock.")
    _row("4  List",      "Agent sets APPROVED → READY. Export CSV (VS Code task) → upload to eBay Seller Hub.")
    _row("5  Monitor",   "Paste eBay URL in col Q → auto-set to ACTIVE. Active Monitor checks stock/price 3×/day.")

    _blank()

    # ── VS CODE TASKS ────────────────────────────────────────────────────────
    _divider("VS CODE TASKS   Ctrl+Shift+P → 'Tasks: Run Task'")
    _row("WAT: Check Status",               "Quick snapshot — last runs, product counts, spot prices, next scheduled run.")
    _row("WAT: Run Discovery",              "Scrape Costco for new products → PENDING. Also runs automatically at 7 AM.")
    _row("WAT: Run Research",               "Score all PENDING rows (eBay comps + Claude). Also runs at 10 AM.")
    _row("WAT: Research — Re-score Only",   "Same as Research but skips discovery. ~2 min. Use after manually adding rows.")
    _row("WAT: Run Daily Sweep",            "APPROVED→READY + PAUSED stock/margin recovery. Also runs at 9 AM.")
    _row("WAT: Run Rotation Digest",        "Weekly: score all WATCH products, flag underperformers. Auto-runs Fridays.")
    _row("WAT: Run Active Monitor",         "LOCAL ONLY — checks stock/price for ACTIVE listings. Needs Chrome open.")
    _row("WAT: Setup Sheet",                "Re-apply dashboard formatting. Safe to re-run anytime sheet looks wrong.")
    _row("WAT: Export eBay CSV",            "Generate upload CSV for READY rows → upload at eBay Seller Hub.")

    return rows, divider_rows, status_rows, col_hdr_rows
```

- [ ] **Step 4: Update `_ensure_legend_tab` to use `_build_legend_rows()`**

In `tools/sheet_formatter.py`, find `_ensure_legend_tab`. Replace the entire section from `rows = []` down to the `service.spreadsheets().values().update(...)` call with:

```python
    rows, divider_rows, status_rows, col_hdr_rows = _build_legend_rows()
```

Keep the existing formatting/API code that follows unchanged — it already uses `rows`, `divider_rows`, `status_rows`, `col_hdr_rows` to apply styles.

- [ ] **Step 5: Update Legend column widths for the new 4-col layout**

In `tools/sheet_formatter.py`, inside `_ensure_legend_tab`, find the column width requests block. Update to the new widths:

```python
        # Column widths: col-letter | header/key | description | notes tag
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 80}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 140}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 380}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
            "properties": {"pixelSize": 90}, "fields": "pixelSize",
        }},
```

- [ ] **Step 6: Style the "HIDDEN COLUMNS" divider differently**

The hidden section divider should use `STATS_BG` (mid-navy) instead of `HDR_BG` (dark navy) to signal it's reference-only.

In `_ensure_legend_tab`, find the divider styling loop:

```python
    for row_idx in divider_rows:
        is_title = (row_idx == 0)
        requests.append({"repeatCell": {
            ...
            "cell": {"userEnteredFormat": {
                "backgroundColor": TITLE_BG if is_title else HDR_BG,
```

The "HIDDEN COLUMNS" divider row index needs to use `STATS_BG`. Since `_build_legend_rows` now returns the rows list, we can identify the hidden section divider by checking the row text. Update the loop:

```python
    for row_idx in divider_rows:
        is_title   = (row_idx == 0)
        is_hidden  = rows[row_idx][0].startswith("HIDDEN COLUMNS")
        bg = TITLE_BG if is_title else (STATS_BG if is_hidden else HDR_BG)
        requests.append({"repeatCell": {
            "range": {"sheetId": legend_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg,
                "textFormat": {
                    "foregroundColor": HDR_FG, "bold": True,
                    "fontSize": 13 if is_title else 10,
                },
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 6, "bottom": 6, "left": 10, "right": 4},
            }},
            "fields": "userEnteredFormat",
        }})
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": legend_id, "dimension": "ROWS",
                       "startIndex": row_idx, "endIndex": row_idx + 1},
            "properties": {"pixelSize": 32 if is_title else 26},
            "fields": "pixelSize",
        }})
```

- [ ] **Step 7: Style "formula" notes in col D with muted italic color**

After the column header rows styling block in `_ensure_legend_tab`, add a pass to style "formula" cells in col D (index 3) with italic muted text:

```python
    # Style "formula" tag cells in col D — italic muted grey
    formula_color = _rgb("78909C")
    for row_idx, row_data in enumerate(rows):
        if len(row_data) > 3 and str(row_data[3]).strip().lower() == "formula":
            requests.append({"repeatCell": {
                "range": {"sheetId": legend_id,
                           "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                           "startColumnIndex": 3, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"italic": True, "foregroundColor": formula_color, "fontSize": 9},
                }},
                "fields": "userEnteredFormat(textFormat)",
            }})
```

- [ ] **Step 8: Run the failing tests to verify they pass**

```
pytest tests/test_menu.py::test_legend_rows_contain_visible_columns_section tests/test_menu.py::test_legend_rows_contain_hidden_columns_section tests/test_menu.py::test_legend_rows_include_all_26_visible_col_letters tests/test_menu.py::test_legend_rows_include_all_22_hidden_col_labels tests/test_menu.py::test_legend_rows_no_old_column_quick_reference -v
```

Expected: PASS.

- [ ] **Step 9: Run full test suite**

```
pytest tests/test_menu.py -v
```

Expected: All tests PASS.

- [ ] **Step 10: Manual verification — run setup_sheet.py**

```
python agents/setup_sheet.py
```

Open the Google Sheet. Verify:
- Product Tracker row 3: unhide columns AA–AV and confirm they have labels (SKU, FEE RATE, eBay FEES, etc.)
- Legend tab: two new sections — "VISIBLE COLUMNS (A–Z)" with 26 rows + "HIDDEN COLUMNS (AA–AV)" with 22 rows
- "HIDDEN COLUMNS" divider is mid-navy (lighter than other section dividers)
- "formula" entries in col D are italic and muted grey
- Old "COLUMN QUICK REFERENCE" section is gone

- [ ] **Step 11: Commit**

```
git add tools/sheet_formatter.py tests/test_menu.py
git commit -m "feat: full column reference in Legend tab — all 48 cols documented, hidden section styled distinctly"
```
