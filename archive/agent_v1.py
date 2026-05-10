"""
ARCHIVE — Original monolithic agent (v1).
DO NOT run this file. It is kept for reference only.
The active system lives in agents/scheduler.py.

Original file: ebay-agent/agent.py
Archived: 2026-05-07 when project was migrated to WAT framework structure.
"""

import os
import json
import time
import schedule
import requests
import anthropic
from datetime import datetime
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ══════════════════════════════════════════════════════════════════
# CONFIG — fill these in before first run
# ══════════════════════════════════════════════════════════════════
CONFIG = {
    # Claude API
    "anthropic_api_key": "ROTATED — see .env for new key",

    # Google Sheets
    "sheet_id": "1KXxULBBp4dmZb1OMGYPkf_YIE1HFd4byQCsAb-_tSic",
    "credentials_json": "google_credentials.json",

    # Alert email (sends to your phone via carrier email-to-SMS gateway)
    "alert_from_email": "jordanleemeece@gmail.com",
    "alert_from_password": "ROTATED — see .env",
    "alert_to_sms": "2818966731@vtext.com",
    "alert_to_email": "jordanleemeece@gmail.com",

    # Your site URLs for redirect messages
    "site_jewelry": "coming-soon.com/jewelry",
    "site_outdoor": "coming-soon.com/outdoor",
    "discount_code": "SAVE10",

    # Run schedule
    "run_times": ["08:00", "13:00", "18:00"],
    "batch_size": 5,
}

# ══════════════════════════════════════════════════════════════════
# SHEET COLUMN MAP  (matches your tracker exactly)
# ══════════════════════════════════════════════════════════════════
COL = {
    "title":        "A",
    "sku":          "B",
    "category":     "C",
    "costco_url":   "D",
    "costco_cost":  "E",
    "ebay_price":   "F",
    "fee_rate":     "G",
    "ebay_fees":    "H",
    "ship_cost":    "I",
    "net_profit":   "J",
    "net_margin":   "K",
    "seo_title":    "L",
    "bullets":      "M",
    "description":  "N",
    "redirect_msg": "O",
    "meta_desc":    "P",
    "keywords":     "Q",
    "alt_text":     "R",
    "sold_90d":     "S",
    "avg_price":    "T",
    "comp_count":   "U",
    "demand_score": "V",
    "last_checked": "W",
    "fulfillment":  "X",
    "stock_status": "Y",
    "tax_est":      "Z",
    "site_profit":  "AA",
    "ad_budget":    "AB",
    "google_hl":    "AC",
    "google_desc":  "AD",
    "meta_text":    "AE",
    "meta_hl":      "AF",
    "status":       "AG",
    "image_urls":   "AH",
    "price_change": "AI",
    "notes":        "AJ",
}

def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(
        CONFIG["credentials_json"], scopes=scopes
    )
    return build("sheets", "v4", credentials=creds)

def read_sheet(service, range_name):
    result = service.spreadsheets().values().get(
        spreadsheetId=CONFIG["sheet_id"],
        range=range_name
    ).execute()
    return result.get("values", [])

def write_cell(service, sheet_name, col, row, value):
    range_addr = f"'{sheet_name}'!{col}{row}"
    service.spreadsheets().values().update(
        spreadsheetId=CONFIG["sheet_id"],
        range=range_addr,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]}
    ).execute()

def write_row_partial(service, sheet_name, row_num, col_value_pairs):
    data = []
    for col, value in col_value_pairs:
        data.append({
            "range": f"'{sheet_name}'!{col}{row_num}",
            "values": [[value]]
        })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=CONFIG["sheet_id"],
            body={"valueInputOption": "USER_ENTERED", "data": data}
        ).execute()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

def scrape_costco(url):
    result = {
        "price": None,
        "stock_status": "Unknown",
        "image_urls": [],
        "title": None,
        "error": None,
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        price_meta = soup.find("meta", {"itemprop": "price"})
        if price_meta:
            result["price"] = float(price_meta.get("content", 0))
        else:
            price_tag = soup.find("div", class_=lambda c: c and "price" in c.lower())
            if price_tag:
                import re
                match = re.search(r'\$[\d,]+\.?\d*', price_tag.get_text())
                if match:
                    result["price"] = float(match.group().replace("$","").replace(",",""))

        out_tags = soup.find_all(string=lambda t: t and any(
            phrase in t.lower() for phrase in
            ["out of stock", "currently unavailable", "sold out"]
        ))
        limited_tags = soup.find_all(string=lambda t: t and any(
            phrase in t.lower() for phrase in ["limited", "while supplies last", "low stock"]
        ))
        if out_tags:
            result["stock_status"] = "OUT OF STOCK"
        elif limited_tags:
            result["stock_status"] = "Limited"
        else:
            result["stock_status"] = "In Stock"

        imgs = soup.find_all("img", class_=lambda c: c and "product" in str(c).lower())
        if not imgs:
            imgs = soup.find_all("img", src=lambda s: s and "costco" in str(s).lower())
        result["image_urls"] = [
            img.get("src") or img.get("data-src", "")
            for img in imgs[:5]
            if img.get("src") or img.get("data-src")
        ]

        h1 = soup.find("h1")
        if h1:
            result["title"] = h1.get_text(strip=True)

    except requests.exceptions.RequestException as e:
        result["error"] = str(e)
        result["stock_status"] = "CHECK FAILED"

    return result

def get_ebay_comps(product_title, category):
    ebay_fee_rates = {
        "Jewelry": 0.15,
        "Outdoor Furniture": 0.115,
        "Toys": 0.1325,
        "Health & Beauty": 0.1325,
    }
    return {
        "avg_sold_price": None,
        "sold_90d": None,
        "comp_count": None,
        "fee_rate": ebay_fee_rates.get(category, 0.1325),
        "note": "Run weekly research prompt in Claude Prompts tab to populate eBay comp data"
    }

def generate_listing_copy(products_batch):
    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])

    product_lines = []
    for i, p in enumerate(products_batch, 1):
        product_lines.append(
            f"{i}. PRODUCT: {p['title']}\n"
            f"   CATEGORY: {p['category']}\n"
            f"   COSTCO COST: ${p['cost']}\n"
            f"   EBAY SELL PRICE: ${p['sell_price']}\n"
            f"   SITE URL: {p['site_url']}\n"
            f"   DISCOUNT CODE: {CONFIG['discount_code']}"
        )

    prompt = f"""You are an expert eBay seller and SEO copywriter. For each product below generate EXACTLY this JSON array. No explanation, no markdown, just the raw JSON.

PRODUCTS:
{chr(10).join(product_lines)}

Return a JSON array with one object per product. Each object must have these exact keys:
- seo_title: eBay title, max 80 chars, keyword-rich, no ALL CAPS
- bullets: exactly 5 bullet points as one string separated by | character
- meta_desc: exactly 155 chars for WordPress SEO
- keywords: 6-8 comma-separated search terms
- alt_text: one descriptive sentence for main product image
- redirect_msg: "Save 10% buying direct at [SITE_URL] with code [CODE] — same product, no eBay fees."
- google_hl: 3 Google ad headlines max 30 chars each, separated by |
- google_desc: 2 Google ad descriptions max 90 chars each, separated by |
- meta_text: Meta/Facebook primary text max 125 chars
- meta_hl: Meta headline max 40 chars
- demand_note: one sentence on why this sells well or risk to watch

JSON array only. Start with [ and end with ]."""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)

def send_alert(subject, body, urgent=False):
    try:
        msg = MIMEMultipart()
        msg["From"] = CONFIG["alert_from_email"]
        msg["Subject"] = f"{'🚨 URGENT — ' if urgent else ''}{subject}"

        msg["To"] = CONFIG["alert_to_email"]
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(CONFIG["alert_from_email"], CONFIG["alert_from_password"])
            server.sendmail(CONFIG["alert_from_email"], CONFIG["alert_to_email"], msg.as_string())

            if urgent:
                sms_msg = MIMEText(f"{subject[:130]}\nCheck sheet now.")
                sms_msg["From"] = CONFIG["alert_from_email"]
                sms_msg["To"] = CONFIG["alert_to_sms"]
                sms_msg["Subject"] = ""
                server.sendmail(CONFIG["alert_from_email"], CONFIG["alert_to_sms"], sms_msg.as_string())

        print(f"  Alert sent: {subject}")
    except Exception as e:
        print(f"  Alert failed: {e}")

def determine_status(stock_status, margin_pct, price_changed, demand_score):
    notes = []
    status = "ACTIVE"

    if stock_status == "OUT OF STOCK":
        status = "URGENT"
        notes.append("OUT OF STOCK — pause listing immediately")
    elif stock_status == "Limited":
        status = "URGENT"
        notes.append("Low stock — monitor closely, consider pausing")
    elif stock_status == "CHECK FAILED":
        status = "URGENT"
        notes.append("Could not reach Costco page — verify manually")

    if price_changed:
        notes.append(f"Price changed since last run — recalculate margin")
        if status == "ACTIVE":
            status = "URGENT"

    if margin_pct is not None and margin_pct < 0.10:
        notes.append(f"Margin below 10% ({margin_pct:.1%}) — not profitable")
        if status == "ACTIVE":
            status = "PAUSED"

    if demand_score is not None and int(demand_score) < 5:
        notes.append("Low demand score — consider removing")

    return status, " | ".join(notes) if notes else "All clear"

def run_agent():
    print(f"\n{'='*60}")
    print(f"Agent run started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    service = get_sheets_service()
    sheet_name = "Product Tracker"

    all_data = read_sheet(service, f"'{sheet_name}'!A4:AJ200")

    urgent_items = []
    products_needing_copy = []
    copy_row_map = []

    for idx, row in enumerate(all_data):
        sheet_row = idx + 4
        if not row or not row[0]:
            continue

        def safe_get(lst, i, default=""):
            return lst[i] if i < len(lst) else default

        title       = safe_get(row, 0)
        sku         = safe_get(row, 1)
        category    = safe_get(row, 2)
        costco_url  = safe_get(row, 3)
        costco_cost = safe_get(row, 4)
        sell_price  = safe_get(row, 5)
        seo_title   = safe_get(row, 11)
        last_checked= safe_get(row, 22)
        current_status = safe_get(row, 32)

        if not costco_url or not costco_url.startswith("http"):
            continue

        print(f"\n  Checking: {title[:50]}...")

        costco_data = scrape_costco(costco_url)
        new_price = costco_data["price"]
        stock_status = costco_data["stock_status"]
        image_urls = " | ".join(costco_data["image_urls"])

        price_changed = False
        if new_price and costco_cost:
            try:
                old_price = float(str(costco_cost).replace("$","").replace(",",""))
                if abs(new_price - old_price) > 0.50:
                    price_changed = True
                    print(f"    Price change: ${old_price} → ${new_price}")
            except (ValueError, TypeError):
                pass

        try:
            margin = float(str(safe_get(row, 10)).replace("%","")) / 100
        except (ValueError, TypeError):
            margin = None
        try:
            demand = int(safe_get(row, 21))
        except (ValueError, TypeError):
            demand = None

        status, notes = determine_status(stock_status, margin, price_changed, demand)

        updates = [
            (COL["stock_status"], stock_status),
            (COL["last_checked"], datetime.now().strftime("%Y-%m-%d %H:%M")),
            (COL["status"], status),
            (COL["notes"], notes),
            (COL["image_urls"], image_urls),
            (COL["price_change"], "YES — update cost" if price_changed else ""),
        ]
        if new_price:
            updates.append((COL["costco_cost"], new_price))

        write_row_partial(service, sheet_name, sheet_row, updates)
        print(f"    Status: {status} | Stock: {stock_status}")

        if status == "URGENT":
            urgent_items.append({
                "title": title,
                "row": sheet_row,
                "status": status,
                "notes": notes,
                "stock": stock_status,
            })

        if not seo_title and costco_cost and sell_price:
            site_url = (CONFIG["site_jewelry"] if category == "Jewelry"
                        else CONFIG["site_outdoor"])
            products_needing_copy.append({
                "title": title,
                "category": category,
                "cost": costco_cost,
                "sell_price": sell_price,
                "site_url": site_url,
            })
            copy_row_map.append(sheet_row)

        time.sleep(2)

    if products_needing_copy:
        print(f"\n  Generating listing copy for {len(products_needing_copy)} products...")
        for i in range(0, len(products_needing_copy), CONFIG["batch_size"]):
            batch = products_needing_copy[i:i + CONFIG["batch_size"]]
            batch_rows = copy_row_map[i:i + CONFIG["batch_size"]]
            try:
                copy_results = generate_listing_copy(batch)
                for j, (result, row_num) in enumerate(zip(copy_results, batch_rows)):
                    copy_updates = [
                        (COL["seo_title"],    result.get("seo_title", "")),
                        (COL["bullets"],      result.get("bullets", "")),
                        (COL["meta_desc"],    result.get("meta_desc", "")),
                        (COL["keywords"],     result.get("keywords", "")),
                        (COL["alt_text"],     result.get("alt_text", "")),
                        (COL["redirect_msg"], result.get("redirect_msg", "")),
                        (COL["google_hl"],    result.get("google_hl", "")),
                        (COL["google_desc"],  result.get("google_desc", "")),
                        (COL["meta_text"],    result.get("meta_text", "")),
                        (COL["meta_hl"],      result.get("meta_hl", "")),
                        (COL["notes"],        result.get("demand_note", "")),
                    ]
                    write_row_partial(service, sheet_name, row_num, copy_updates)
                    print(f"    Copy written for row {row_num}")
                time.sleep(1)
            except Exception as e:
                print(f"    Copy generation failed for batch: {e}")

    if urgent_items:
        subject = f"{len(urgent_items)} listing(s) need immediate action"
        lines = [f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
        for item in urgent_items:
            lines.append(f"ROW {item['row']}: {item['title']}")
            lines.append(f"  Status: {item['status']}")
            lines.append(f"  Stock:  {item['stock']}")
            lines.append(f"  Notes:  {item['notes']}\n")
        lines.append("Open your Google Sheet to take action:")
        lines.append(f"https://docs.google.com/spreadsheets/d/{CONFIG['sheet_id']}/edit")
        body = "\n".join(lines)
        send_alert(subject, body, urgent=True)

    print(f"\n  Run complete. {len(urgent_items)} urgent item(s).")

if __name__ == "__main__":
    print("Costco → eBay Agent starting...")
    print(f"Scheduled run times: {CONFIG['run_times']}")

    for run_time in CONFIG["run_times"]:
        schedule.every().day.at(run_time).do(run_agent)

    run_agent()

    while True:
        schedule.run_pending()
        time.sleep(60)
