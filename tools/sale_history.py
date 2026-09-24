"""
Sale History
============
Append-only "Sale History" tab: one row each time the Costco scraper sees a product on
sale at a NEW price (or again after a gap), so a later phase can predict sale cycles.

Row: [product_title, category, scrape_date, sale_price, regular_price, sale_end_date,
      coupon_type, coupon_label]
coupon_type = "MFR" (manufacturer coupon) / "STORE" (Costco instant savings) / "OTHER", from the
price API's promotion text; blank when unknown (rows written before the columns existed, or a
sale detected only from the page DOM). Old rows are not migrated.

Dedup: a live sale must not add a row on every research/recheck run, so a row is skipped
when the tab already holds one for the same product with the same sale price and a
scrape_date within DEDUP_DAYS. A new price, or a gap, appends again.

log_sale() NEVER raises — a Sale History failure must not break a research run.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta

from googleapiclient.errors import HttpError
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.sheet_writer import execute_with_retry  # noqa: E402

TAB_NAME   = "Sale History"
HEADER     = ["PRODUCT_TITLE", "CATEGORY", "SCRAPE_DATE", "SALE_PRICE",
              "REGULAR_PRICE", "SALE_END_DATE", "COUPON_TYPE", "COUPON_LABEL"]
_header_checked = False   # per process: the live tab's header is topped up once
DEDUP_DAYS = 7
PRICE_EPS  = 0.005

_END_RE = re.compile(r"ends?\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", re.IGNORECASE)


def _money(value):
    """'$1,299.00' / 1299 -> float; blank/garbage -> None."""
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def parse_sale_end(sale_info, today=None) -> str:
    """
    'ends 5/31/26' / 'ends 5/31/2026' / 'ends 5/31' -> 'YYYY-MM-DD'; unparseable -> ''.
    A year-less date takes this year, or next year when that date is already more than
    30 days past (a sale badge read in December that says 'ends 1/5').
    """
    m = _END_RE.search(str(sale_info or ""))
    if not m:
        return ""
    today = today or date.today()
    month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day).isoformat()
        d = date(today.year, month, day)
        if (today - d).days > 30:
            d = date(today.year + 1, month, day)
        return d.isoformat()
    except ValueError:
        return ""


def should_append(existing_rows, title, sale_price, today=None, window_days=DEDUP_DAYS) -> bool:
    """
    existing_rows: tab rows as read back (title, category, scrape_date, sale_price, ...).
    False iff a row has the same title (case-insensitive), the same sale price (±half a
    cent) and a scrape_date within window_days of today.
    """
    today = today or date.today()
    want = str(title).strip().lower()
    price = _money(sale_price)
    for r in existing_rows:
        if len(r) < 4 or str(r[0]).strip().lower() != want:
            continue
        seen_price = _money(r[3])
        if price is None or seen_price is None or abs(seen_price - price) > PRICE_EPS:
            continue
        try:
            seen = datetime.strptime(str(r[2]).strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if abs((today - seen).days) <= window_days:
            return False
    return True


def _sheet_id():
    sid = os.getenv("GOOGLE_SHEET_ID")
    if not sid:
        raise RuntimeError("GOOGLE_SHEET_ID not set in environment.")
    return sid


def _upgrade_header(service, sid):
    """Tabs created before COUPON_TYPE/COUPON_LABEL existed get the two header cells added
    (existing rows keep blank cells — no migration). Once per process; never raises."""
    global _header_checked
    if _header_checked:
        return
    _header_checked = True
    try:
        got = execute_with_retry(service.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{TAB_NAME}'!A1:H1"), "sale_history header read")
        have = (got.get("values") or [[]])[0]
        if len(have) < len(HEADER):
            execute_with_retry(service.spreadsheets().values().update(
                spreadsheetId=sid, range=f"'{TAB_NAME}'!A1", valueInputOption="RAW",
                body={"values": [HEADER]}), "sale_history header upgrade")
            logger.info("Sale History header upgraded (COUPON_TYPE, COUPON_LABEL).")
    except Exception as e:
        logger.warning(f"Sale History header check failed (non-fatal): {e}")


def ensure_tab(service) -> bool:
    """Create the Sale History tab (with header) if missing. Idempotent. True if created."""
    sid = _sheet_id()
    meta = execute_with_retry(service.spreadsheets().get(
        spreadsheetId=sid, fields="sheets.properties.title"), "sale_history meta")
    if TAB_NAME in {s["properties"]["title"] for s in meta.get("sheets", [])}:
        _upgrade_header(service, sid)
        return False
    try:
        # addSheet is non-idempotent: a landed 5xx/timeout retried would 400 on the duplicate
        execute_with_retry(service.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": TAB_NAME}}}]},
        ), "sale_history addSheet", retry_statuses=(429,), retry_timeouts=False)
    except HttpError as e:
        if "already exists" in str(e):     # lost a race with another run — tab is there
            return False
        raise
    execute_with_retry(service.spreadsheets().values().update(
        spreadsheetId=sid, range=f"'{TAB_NAME}'!A1", valueInputOption="RAW",
        body={"values": [HEADER]},
    ), "sale_history headers")
    logger.info("Sale History tab created.")
    return True


def log_sale(service, title, category, sale_price, regular_price, sale_info, today=None,
             coupon_type="", coupon_label="") -> bool:
    """
    Append one Sale History row when sale_info (col X) is non-blank and it isn't a repeat
    (see should_append). sale_price = Costco price at scrape time; regular_price may be
    blank; coupon_type/label come from the scraper ("" = unknown). Returns True only if a
    row was appended. Never raises.
    """
    try:
        if not str(sale_info or "").strip():
            return False
        today = today or date.today()
        ensure_tab(service)
        result = execute_with_retry(service.spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range=f"'{TAB_NAME}'!A2:D"), "sale_history read")
        if not should_append(result.get("values", []), title, sale_price, today):
            return False
        price = _money(sale_price)
        reg = _money(regular_price)
        row = [str(title), str(category or ""), today.isoformat(),
               price if price is not None else "",
               reg if reg is not None else "",
               parse_sale_end(sale_info, today),
               str(coupon_type or ""), str(coupon_label or "")]
        execute_with_retry(service.spreadsheets().values().append(
            spreadsheetId=_sheet_id(), range=f"'{TAB_NAME}'!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ), "sale_history append")
        logger.info(f"Sale History: logged {str(title)[:40]} @ ${price}")
        return True
    except Exception as e:  # contract: a Sale History failure never breaks a run
        logger.warning(f"Sale History write failed (non-fatal): {e}")
        return False
