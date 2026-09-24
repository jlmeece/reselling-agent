"""
eBay sync — Stage 1 (read-only on eBay, flag-only on the sheet)
===============================================================
Pulls Jay's active eBay listings via the Trading API (GetMyeBaySelling), matches
them to Product Tracker rows by the item ID in col Q (ebay_listing_url), and:

  * writes QuantitySold into units_sold (col U) — the ONLY write, and only when it
    changed (through tools.sheet_writer.safe_write_row);
  * REPORTS (never fixes) price mismatches, eBay listings missing from the sheet,
    and ACTIVE sheet rows that eBay no longer lists.

Never touches eBay data and never changes any price. Run via
`python agents/scheduler.py --mode ebay_sync [--dry-run]`.

Known Stage-1 limit: GetMyeBaySelling's ActiveList only holds live listings, so a
listing that sold out completely drops off it. Its last sale is therefore NOT
written to units_sold and the row surfaces under active_not_on_ebay ("sold out /
removed / never listed"). Stage 2 would add the SoldList to tell those apart.
"""

import html
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

import yaml
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import read_sheet, safe_write_row  # noqa: E402

ENDPOINT          = "https://api.ebay.com/ws/api.dll"
COMPAT_LEVEL      = "1193"
ENTRIES_PER_PAGE  = 200
MAX_PAGES         = 50          # runaway guard (10,000 listings)
REQUEST_TIMEOUT   = 30
PRICE_TOLERANCE   = 0.01        # eBay vs sheet price differences below 1¢ are rounding
WRITE_DELAY       = 1.1         # s between sheet writes — Sheets caps writes at 60/min
MAX_REPORT_LINES  = 15          # per section in the Telegram message

# Trading API error codes meaning the auth token is invalid/expired (tokens last
# ~18 months, so this needs a loud alert rather than a silent empty sync).
AUTH_ERROR_CODES = {"931", "932", "16110"}

_NS = "urn:ebay:apis:eBLBaseComponents"
_sleep = time.sleep  # indirection so tests don't really sleep

# Outcome of the most recent fetch_active_listings() call. fetch never raises and
# returns [] on failure, so callers read this to tell "no listings" from "failed".
# kind: None (ok) | "no_token" | "no_credentials" | "auth" | "api" | "network"
last_error = {"kind": None, "message": ""}


def get_last_error() -> dict:
    return dict(last_error)


def _set_error(kind, message=""):
    last_error["kind"] = kind
    last_error["message"] = message


# ── Item ID extraction ────────────────────────────────────────────────────────

_BARE_ID_RE = re.compile(r"^\d{9,14}$")
_ITM_PATH_RE = re.compile(r"/itm/(?:[^/?#\s]+/)?(\d{9,14})(?!\d)", re.IGNORECASE)
_ITEM_PARAM_RE = re.compile(r"[?&]item=(\d{9,14})(?!\d)", re.IGNORECASE)


def extract_item_id(value) -> str | None:
    """
    Normalise a col Q value to a bare numeric eBay item ID, or None.

      "https://www.ebay.com/itm/<ID>"              -> "<ID>"
      "ebay.com/itm/<slug>/<ID>?hash=item..."      -> "<ID>"
      "<ID>" (9-14 digits)                         -> "<ID>"

    Anything else (blank, formulas, non-eBay URLs, short/long digit runs) is None.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.startswith("="):
        return None
    if _BARE_ID_RE.match(s):
        return s
    if "ebay." not in s.lower():
        return None
    m = _ITM_PATH_RE.search(s) or _ITEM_PARAM_RE.search(s)
    return m.group(1) if m else None


# ── eBay Trading API ──────────────────────────────────────────────────────────

def _build_request_xml(token: str, page: int) -> str:
    selectors = "".join(f"<OutputSelector>{s}</OutputSelector>" for s in (
        "ActiveList.ItemArray.Item.ItemID",
        "ActiveList.ItemArray.Item.Title",
        "ActiveList.ItemArray.Item.Quantity",
        "ActiveList.ItemArray.Item.SellingStatus",
        "ActiveList.ItemArray.Item.WatchCount",
        "ActiveList.ItemArray.Item.HitCount",
        "ActiveList.PaginationResult",
    ))
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<GetMyeBaySellingRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        "<ActiveList><Include>true</Include>"
        f"<Pagination><EntriesPerPage>{ENTRIES_PER_PAGE}</EntriesPerPage>"
        f"<PageNumber>{page}</PageNumber></Pagination></ActiveList>"
        f"{selectors}"
        "</GetMyeBaySellingRequest>"
    )


def _headers(app_id: str, dev_id: str, cert_id: str) -> dict:
    return {
        "Content-Type": "text/xml; charset=utf-8",
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
        "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-APP-NAME": app_id,
        "X-EBAY-API-DEV-NAME": dev_id,
        "X-EBAY-API-CERT-NAME": cert_id,
    }


def _post(body: str, headers: dict) -> bytes:
    """POST to the Trading API; one retry on timeout / connection error / HTTP 5xx.
    Raises OSError (URLError/HTTPError/timeout) if both attempts fail."""
    req = urllib.request.Request(ENDPOINT, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
            logger.warning(f"ebay_sync: HTTP {e.code} from eBay — retrying once")
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            if attempt == 2:
                raise
            logger.warning(f"ebay_sync: eBay request failed ({e}) — retrying once")
        _sleep(2)


def _child(el, tag):
    return el.find(f"{{*}}{tag}")


def _text(el, tag, default=""):
    c = _child(el, tag)
    return (c.text or "").strip() if c is not None and c.text else default


def _num(text, cast, default=0):
    try:
        return cast(float(text)) if cast is int else cast(text)
    except (TypeError, ValueError):
        return default


def _parse_errors(root) -> list[tuple[str, str]]:
    out = []
    for err in root.findall("{*}Errors"):
        if _text(err, "SeverityCode") == "Warning":
            continue
        out.append((_text(err, "ErrorCode"), _text(err, "ShortMessage") or _text(err, "LongMessage")))
    return out


def _parse_listing(item) -> dict | None:
    item_id = _text(item, "ItemID")
    if not item_id:
        return None
    status = _child(item, "SellingStatus")
    price = None
    qty_sold = 0
    if status is not None:
        price = _num(_text(status, "CurrentPrice"), float, None)
        qty_sold = _num(_text(status, "QuantitySold"), int, 0)
    return {
        "item_id":       item_id,
        "title":         _text(item, "Title"),
        "price":         price,
        "quantity":      _num(_text(item, "Quantity"), int, 0),
        "quantity_sold": qty_sold,
        "watch_count":   _num(_text(item, "WatchCount"), int, 0),
        # The Trading API item field is HitCount; ViewCount kept as a fallback name.
        "view_count":    _num(_text(item, "HitCount") or _text(item, "ViewCount"), int, 0),
    }


def fetch_active_listings() -> list[dict]:
    """
    Fetch every active eBay listing (paged, 200/page).

    Returns [{item_id, title, price, quantity, quantity_sold, watch_count,
    view_count}, ...]. NEVER raises: on any failure — missing credentials, network
    error, API Ack=Failure, bad XML — it logs and returns [], and sets
    `last_error` so the caller can tell that apart from "no listings". A failure
    on a later page discards earlier pages too: a partial list would make every
    unfetched listing look "removed from eBay".
    """
    _set_error(None)
    try:
        token   = os.getenv("EBAY_AUTH_TOKEN", "").strip()
        if not token:
            logger.warning("ebay_sync skipped — no EBAY_AUTH_TOKEN")
            _set_error("no_token", "ebay_sync skipped — no EBAY_AUTH_TOKEN")
            return []
        app_id  = os.getenv("EBAY_APP_ID", "").strip()
        dev_id  = os.getenv("EBAY_DEV_ID", "").strip()
        cert_id = os.getenv("EBAY_CERT_ID", "").strip()
        missing = [n for n, v in (("EBAY_APP_ID", app_id), ("EBAY_DEV_ID", dev_id),
                                  ("EBAY_CERT_ID", cert_id)) if not v]
        if missing:
            msg = f"ebay_sync skipped — missing {', '.join(missing)}"
            logger.warning(msg)
            _set_error("no_credentials", msg)
            return []

        headers  = _headers(app_id, dev_id, cert_id)
        listings, seen = [], set()
        page, total_pages = 1, 1
        while page <= total_pages and page <= MAX_PAGES:
            try:
                raw = _post(_build_request_xml(token, page), headers)
            except OSError as e:
                msg = f"eBay request failed: {e}"
                logger.error(f"ebay_sync: {msg}")
                _set_error("network", msg)
                return []
            try:
                root = ET.fromstring(raw)
            except ET.ParseError as e:
                msg = f"unparseable eBay response: {e}"
                logger.error(f"ebay_sync: {msg}")
                _set_error("api", msg)
                return []

            ack = _text(root, "Ack")
            if ack not in ("Success", "Warning"):
                errors = _parse_errors(root)
                detail = "; ".join(f"{c}: {m}" for c, m in errors) or f"Ack={ack or 'missing'}"
                logger.error(f"ebay_sync: eBay API error — {detail}")
                kind = "auth" if any(c in AUTH_ERROR_CODES for c, _ in errors) else "api"
                _set_error(kind, detail)
                return []

            active = _child(root, "ActiveList")
            if active is not None:
                pag = _child(active, "PaginationResult")
                if pag is not None:
                    total_pages = _num(_text(pag, "TotalNumberOfPages"), int, 1) or 1
                for item in active.findall("{*}ItemArray/{*}Item"):
                    listing = _parse_listing(item)
                    if listing and listing["item_id"] not in seen:
                        seen.add(listing["item_id"])
                        listings.append(listing)
            page += 1

        logger.info(f"ebay_sync: fetched {len(listings)} active eBay listing(s)")
        return listings
    except Exception as e:  # contract: never raises
        logger.error(f"ebay_sync: unexpected error fetching listings: {e}")
        _set_error("api", f"unexpected error: {e}")
        return []


# ── Sheet side ────────────────────────────────────────────────────────────────

def _col_idx(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _cell(row, idx):
    return row[idx] if idx < len(row) else ""


def _to_float(value):
    """'$29.99' / '1,299.00' -> float; blank/garbage -> None (un-priced, not $0)."""
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _to_int(value) -> int:
    f = _to_float(value)
    return int(f) if f is not None else 0


def load_sheet_rows(service, COL, sheet_name, start_row, end_row) -> list[dict]:
    """Read the tracker into dicts (row_num = absolute sheet row). Skips empty rows."""
    raw = read_sheet(service, f"'{sheet_name}'!A{start_row}:BA{end_row}")
    idx = {k: _col_idx(COL[k]) for k in
           ("status", "title", "platform", "ebay_price", "ebay_listing_url", "units_sold")}
    rows = []
    for offset, r in enumerate(raw):
        row = {k: _cell(r, i) for k, i in idx.items()}
        if not any(str(v).strip() for v in row.values()):
            continue
        row["row_num"] = start_row + offset
        rows.append(row)
    return rows


def _read_titles(service, COL, sheet_name, start_row, end_row) -> dict:
    """{row_num: title} — a cheap re-read used to detect rows shifted by the auditor."""
    col = COL["title"]
    raw = read_sheet(service, f"'{sheet_name}'!{col}{start_row}:{col}{end_row}")
    return {start_row + i: (r[0] if r else "") for i, r in enumerate(raw)}


def _load_defaults():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "config", "col_map.yaml"), encoding="utf-8") as f:
        col_map = yaml.safe_load(f)["columns"]
    with open(os.path.join(base, "config", "categories.yaml"), encoding="utf-8") as f:
        sheet_name = yaml.safe_load(f)["business"]["sheet_name"]
    return sheet_name, col_map


# ── Matching / reporting ──────────────────────────────────────────────────────

def sync(service, sheet_rows, listings, *, dry_run=False, sheet_name=None,
         col_map=None, title_reader=None) -> dict:
    """
    Match listings to sheet rows by the item ID in col Q. Flag-only: units_sold
    (col U) is the only thing ever written, and only when QuantitySold differs.

    sheet_rows:    dicts from load_sheet_rows (row_num, title, status, platform,
                   ebay_price, ebay_listing_url, units_sold)
    title_reader:  optional callable -> {row_num: title}; called once before the
                   first write. A row whose title no longer matches (auditor deleted
                   rows since the read) is skipped, not written.

    Returns {matched, updated, price_mismatch, on_ebay_not_in_sheet,
             active_not_on_ebay, duplicate_url, stale_rows, write_errors, dry_run}.
    `matched` = every matched row; `updated` = the subset whose col U changed.
    """
    if sheet_name is None or col_map is None:
        d_sheet, d_cols = _load_defaults()
        sheet_name = sheet_name or d_sheet
        col_map = col_map or d_cols
    units_col = col_map["units_sold"]

    report = {"matched": [], "updated": [], "price_mismatch": [],
              "on_ebay_not_in_sheet": [], "active_not_on_ebay": [],
              "duplicate_url": [], "stale_rows": [], "write_errors": [],
              "dry_run": dry_run}

    by_id = {l["item_id"]: l for l in listings}
    claimed = set()
    to_write = []

    for row in sheet_rows:
        item_id = extract_item_id(row.get("ebay_listing_url"))
        title = row.get("title", "")
        listing = by_id.get(item_id) if item_id else None

        if listing is not None:
            if item_id in claimed:
                report["duplicate_url"].append(
                    {"row_num": row["row_num"], "title": title, "item_id": item_id})
                continue
            claimed.add(item_id)
            old_sold = _to_int(row.get("units_sold"))
            new_sold = listing["quantity_sold"]
            entry = {
                "row_num": row["row_num"], "title": title, "item_id": item_id,
                "old_units_sold": old_sold, "quantity_sold": new_sold,
                "available": max(listing["quantity"] - new_sold, 0),
                "watch_count": listing["watch_count"], "view_count": listing["view_count"],
                "price": listing["price"],
            }
            report["matched"].append(entry)
            if new_sold != old_sold:
                report["updated"].append(entry)
                to_write.append(entry)
                if new_sold < old_sold:
                    logger.warning(f"ebay_sync: row {row['row_num']} units_sold "
                                   f"{old_sold} -> {new_sold} (eBay count went down)")

            sheet_price = _to_float(row.get("ebay_price"))
            if (sheet_price is not None and listing["price"] is not None
                    and abs(listing["price"] - sheet_price) >= PRICE_TOLERANCE):
                report["price_mismatch"].append({
                    "row_num": row["row_num"], "title": title, "item_id": item_id,
                    "ebay_price": listing["price"], "sheet_price": sheet_price})
        elif (str(row.get("status", "")).strip().upper() == "ACTIVE"
              and str(row.get("platform", "")).strip().lower() in ("", "ebay", "both")):
            if item_id:
                reason = "not in eBay active listings (sold out / removed)"
            elif str(row.get("ebay_listing_url", "")).strip():
                reason = "col Q has no parseable eBay item ID"
            else:
                reason = "no eBay listing URL in col Q (never listed?)"
            report["active_not_on_ebay"].append(
                {"row_num": row["row_num"], "title": title, "item_id": item_id, "reason": reason})

    sheet_ids = {extract_item_id(r.get("ebay_listing_url")) for r in sheet_rows}
    report["on_ebay_not_in_sheet"] = [l for l in listings if l["item_id"] not in sheet_ids]

    if to_write and not dry_run:
        _write_units_sold(service, sheet_name, units_col, to_write, report, title_reader)
    return report


def _write_units_sold(service, sheet_name, units_col, entries, report, title_reader):
    live_titles = None
    if title_reader is not None:
        try:
            live_titles = title_reader()
        except Exception as e:
            logger.error(f"ebay_sync: could not re-verify row titles, skipping writes: {e}")
            report["write_errors"].append({"row_num": None, "title": "", "error": f"title re-check failed: {e}"})
            return

    first = True
    for entry in entries:
        if live_titles is not None and live_titles.get(entry["row_num"], "") != entry["title"]:
            logger.warning(f"ebay_sync: row {entry['row_num']} changed since read — skipped")
            report["stale_rows"].append({"row_num": entry["row_num"], "title": entry["title"]})
            continue
        if not first:
            _sleep(WRITE_DELAY)
        first = False
        try:
            safe_write_row(service, sheet_name, entry["row_num"],
                           [(units_col, entry["quantity_sold"])])
        except Exception as e:
            logger.error(f"ebay_sync: write failed for row {entry['row_num']}: {e}")
            report["write_errors"].append(
                {"row_num": entry["row_num"], "title": entry["title"], "error": str(e)[:150]})


def summarize(report: dict) -> str:
    """One-line summary for the Run Log notes column."""
    s = (f"matched {len(report['matched'])}, updated {len(report['updated'])}, "
         f"price_mismatch {len(report['price_mismatch'])}, "
         f"active_not_on_ebay {len(report['active_not_on_ebay'])}, "
         f"not_in_sheet {len(report['on_ebay_not_in_sheet'])}")
    if report.get("stale_rows"):
        s += f", stale {len(report['stale_rows'])}"
    if report.get("write_errors"):
        s += f", write_errors {len(report['write_errors'])}"
    return ("[dry-run] " if report.get("dry_run") else "") + s


def _truncate(s, n=50):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _section(header, lines):
    out = [header]
    out += lines[:MAX_REPORT_LINES]
    if len(lines) > MAX_REPORT_LINES:
        out.append(f"…and {len(lines) - MAX_REPORT_LINES} more")
    return out


def alert_message(report: dict) -> str | None:
    """
    Telegram HTML message, or None when there is nothing to flag. Only price
    mismatches and ACTIVE-but-not-on-eBay rows warrant a message; everything else
    stays in the Run Log. Sheet-derived titles are truncated THEN html-escaped.
    """
    mism, gone = report["price_mismatch"], report["active_not_on_ebay"]
    if not mism and not gone:
        return None
    msg = ["🛒 <b>eBay sync</b>" + (" (dry run)" if report.get("dry_run") else "")]
    if mism:
        msg += _section(f"\n💲 <b>Price mismatch ({len(mism)})</b>", [
            f"• {html.escape(_truncate(m['title']))} — eBay ${m['ebay_price']:.2f} vs sheet "
            f"${m['sheet_price']:.2f} (row {m['row_num']})" for m in mism])
    if gone:
        msg += _section(f"\n⚠️ <b>ACTIVE but not on eBay ({len(gone)})</b>", [
            f"• {html.escape(_truncate(g['title']))} — {html.escape(g['reason'])} (row {g['row_num']})"
            for g in gone])
    extra = len(report["on_ebay_not_in_sheet"])
    if extra:
        msg.append(f"\nℹ️ {extra} eBay listing(s) not in the sheet")
    return "\n".join(msg)


# ── Orchestration (called by agents/scheduler.py --mode ebay_sync) ────────────

def run_ebay_sync(config, COL, service, sheet_name, start_row, end_row, dry_run=False) -> dict:
    """
    Fetch -> load rows -> sync. Returns run-result keys for the scheduler's Run Log
    (status, notes, errors) plus `alert` (Telegram text or None). Never raises for
    eBay-side problems; on API failure it does NOT compute active_not_on_ebay
    (an outage must not flag every ACTIVE row as removed).
    """
    listings = fetch_active_listings()
    err = get_last_error()
    kind = err["kind"]
    if kind in ("no_token", "no_credentials"):
        return {"status": "skipped", "notes": err["message"], "alert": None}
    if kind:
        alert = None
        if kind == "auth":
            alert = ("🔑 <b>eBay sync: auth token rejected</b> — "
                     f"{html.escape(err['message'][:200])}\nGenerate a new EBAY_AUTH_TOKEN.")
        return {"status": "error", "errors": f"eBay API: {err['message']}"[:300], "alert": alert}

    rows = load_sheet_rows(service, COL, sheet_name, start_row, end_row)
    report = sync(
        service, rows, listings, dry_run=dry_run, sheet_name=sheet_name, col_map=COL,
        title_reader=lambda: _read_titles(service, COL, sheet_name, start_row, end_row),
    )
    result = {"status": "ok", "notes": summarize(report), "alert": alert_message(report)}
    if report["write_errors"]:
        result["status"] = "error"
        result["errors"] = "; ".join(
            f"row {w['row_num']}: {w['error']}" for w in report["write_errors"])[:300]
    logger.info(f"ebay_sync: {result['notes']}")
    return result
