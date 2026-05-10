# Workflow: Monitor
**Layer:** Workflow (Layer 1 — Instructions)
**Agent reads this:** Yes — this is the SOP for the monitoring cycle
**Runs:** 3x daily at 08:00, 13:00, 18:00 (configured in config/categories.yaml)
**Entry point:** `python agents/scheduler.py`

---

## Objective

Check every active Costco product URL for price changes and stock status. Update the Google Sheet with current data. Generate listing copy for any new products that don't have it yet. Fire an URGENT alert for anything needing immediate human attention.

---

## Required Inputs

- Google Sheet: Product Tracker tab, rows 4–200
- Columns needed: A (title), C (category), D (costco_url), E (costco_cost), F (ebay_price), K (net_margin), L (seo_title), V (demand_score)
- `.env` must be populated with: ANTHROPIC_API_KEY, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_FILE, ALERT_FROM_EMAIL, ALERT_FROM_PASSWORD, ALERT_TO_SMS, ALERT_TO_EMAIL

---

## Tools Used (in order)

1. `tools/sheet_writer.py` — `read_sheet()` to load all product rows
2. `tools/costco_scraper.py` — `scrape_costco(url)` for each product
3. `tools/status_logic.py` — `determine_status()` for each scraped result
4. `tools/sheet_writer.py` — `write_row_partial()` to write updates back
5. `tools/listing_copy.py` — `generate_listing_copy(batch)` for new products only
6. `tools/alert_sender.py` — `send_alert()` if any URGENT items found

---

## Step-by-Step Process

### Step 1: Load products
Read all rows from `'Product Tracker'!A4:AJ200`. Skip rows with no title or no Costco URL.

### Step 2: Scrape each product
For each product with a valid Costco URL (starts with "http"):
- Call `scrape_costco(url)` — returns price, stock_status, image_urls, title
- Wait 2 seconds between requests (polite delay — do not remove)

**Handle errors:** If `stock_status` is "CHECK FAILED", write it to the sheet and add to urgent list. Do not retry on same run.

### Step 3: Detect price changes
Compare the scraped price against the value in column E (costco_cost).
- Threshold: flag as changed if difference > $0.50
- Write "YES — update cost" to column AI (price_change) if flagged

### Step 4: Determine status
Call `determine_status(stock_status, margin, price_changed, demand_score)`.
- Status values: ACTIVE / PAUSED / URGENT
- URGENT triggers: OUT OF STOCK, Limited stock, CHECK FAILED, price change, margin < 10%
- PAUSED triggers: margin below 10%

### Step 5: Write updates to sheet
Write these columns for every processed row:
- Y: stock_status
- W: last_checked (timestamp)
- AG: status (ACTIVE/PAUSED/URGENT)
- AJ: notes (human-readable reason)
- AH: image_urls (pipe-separated Costco image URLs for manual download)
- AI: price_change flag
- E: new costco_cost (if price changed)

Use `write_row_partial()` — batch writes, one API call per row.

### Step 6: Generate listing copy (new products only)
Collect all rows where column L (seo_title) is empty AND columns E and F are populated.
- Batch in groups of 5 (batch_size from categories.yaml)
- Call `generate_listing_copy(batch)` for each batch
- Write results to columns L through AF
- Wait 1 second between batches

**If copy generation fails:** Log the error, skip that batch, continue. Do not retry on same run. Next run will pick up failed rows automatically (seo_title will still be empty).

### Step 7: Send urgent alert
If any products have URGENT status:
- Call `send_alert(subject, body, urgent=True)`
- Subject: "{count} listing(s) need immediate action"
- Body: list each urgent product with row number, title, status, stock, notes
- Include direct link to the Google Sheet

---

## Status Definitions

| Status | Meaning | Action needed |
|--------|---------|---------------|
| ACTIVE | All clear — listing is healthy | None |
| PAUSED | Margin below 10% — not profitable | Review pricing before listing |
| URGENT | Out of stock, price spike, or check failed | Immediate attention required |

---

## Edge Cases

- **Product URL returns 404:** Mark CHECK FAILED, add to urgent list
- **Price scraped as None:** Do not overwrite column E — skip price update
- **Costco blocks the request:** Same as CHECK FAILED — this happens occasionally, next run usually succeeds
- **Sheet row has no category:** Use default fee rate (13.25%), skip category-specific site URL
- **Claude API fails on copy batch:** Log error, skip batch, rows will be retried on next run

---

## Self-Improvement Notes

*Update this section when you encounter new issues or find better approaches.*

- 2-second delay between Costco requests prevents soft rate-limiting
- Batch size of 5 for Claude API is the sweet spot: large enough to benefit from caching, small enough to keep response JSON clean
- Google Sheets batchUpdate is used for partial row writes — never write entire rows, it overwrites formula columns (H, J, K, Z, AA, AB)
