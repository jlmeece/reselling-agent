"""
eBay sync — sheet follows eBay (col U units sold, col H live price) + guarded eBay writes
===============================================================
Pulls Jay's active eBay listings via the Trading API (GetMyeBaySelling), matches
them to Product Tracker rows by the item ID in col Q (ebay_listing_url), and:

  * writes QuantitySold into units_sold (col U) when it changed — minus units in orders
    the sales log knows were cancelled/refunded — and, when a NEW sale lands, snapshots
    costco_cost (col G) into buy_cost (col BB) if buy_cost is blank (a legacy snapshot:
    margin math prefers the current col G, so BB is only a fallback when G is blank);
  * MIRRORS eBay's live Buy It Now price into ebay_price (col H) on matched ACTIVE rows
    (mirror_live_prices — runs last, skips items repriced this run), so a bulk edit in
    Seller Hub flows into the sheet. Price only: col BE (manual_cost) is never touched;
  * REPORTS (never fixes) margin breaches (live eBay price vs cost — NOT raw price
    movement; Jay undercuts by a cent himself), eBay listings missing from the sheet,
    and ACTIVE sheet rows that eBay no longer lists.

Margin check: net = live price - cost_basis - (price*fee_rate*1.08 + 0.30) - ship - ad
- cost_basis*0.0825 (Costco sales tax), where cost_basis = costco_cost (col G, today's
Costco price = what a drop-shipper pays at sale time) if present and > 0, else buy_cost
(col BB, fallback). Matches the sheet's col I formula (H-G-AC-AD-AE-AF).
HARD (Telegram per run): net < 0. SOFT (once-a-day digest): 0 <= net < $4 AND
sold_90d == 0. Everything else is silent.

The sync itself never changes anything on eBay; the sheet follows eBay (col U, col H).
Run via `python agents/scheduler.py --mode ebay_sync [--dry-run]`.

The eBay writes in this module are revise_fixed_price() (ReviseFixedPriceItem),
set_quantity() (ReviseInventoryStatus) and end_fixed_price() (EndItem, unused). Prices change
only for: a Telegram tap ("✓ Reprice" / a scheduled sale-end action), a price Jay typed in
col BD (apply_target_prices — every ebay_sync run, or the bot's "Apply price changes now"),
or auto_reprice_breaches (losing listings -> break-even, skipped while col BE says Jay priced
it manually against the current cost). fetch_item_price() (GetItem) is the read-only guard.

Known Stage-1 limit: GetMyeBaySelling's ActiveList only holds live listings, so a
listing that sold out completely drops off it. Its last sale is therefore NOT
written to units_sold and the row surfaces under active_not_on_ebay ("sold out /
removed"). Stage 2 would add the SoldList to tell those apart.

ACTIVE rows with a blank col Q are reported as "no URL in col Q" (the item may well be
on eBay, just unlinked). When an unclaimed eBay listing's title matches the row, the
report suggests its item ID — a suggestion only; col Q is never written.
"""

import hashlib
import html
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as _xml_escape

import yaml
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import (  # noqa: E402
    ensure_grid_columns, read_sheet, required_grid_columns, safe_write_row,
)

ENDPOINT          = "https://api.ebay.com/ws/api.dll"
COMPAT_LEVEL      = "1193"
ENTRIES_PER_PAGE  = 200
MAX_PAGES         = 50          # runaway guard (10,000 listings)
REQUEST_TIMEOUT   = 30
MARGIN_SOFT_FLOOR = 4.00        # net below this AND sold_90d == 0 → SOFT (daily digest)
# Margin-floor alert (alert only, never auto-reprice): a live listing whose eBay price is
# >= FLOOR_PRICE and whose net margin is below FLOOR_MARGIN_PCT is flagged "floor" so Jay can
# reprice it up competitively himself. Tuned via business.ebay_sync.floor_price /
# floor_margin_pct in config/categories.yaml (these are the defaults).
FLOOR_PRICE      = 70.0         # eBay price at/above which the floor applies
FLOOR_MARGIN_PCT = 0.09         # flag when net margin falls below this (9%)
# Costco tax + eBay fee constants — keep in sync with the sheet formula
# (formula_seeder.py / reseed_formulas.py / setup_sheet.py):
#   AC (eBay fee) = H*AB*1.08 + 0.30;   AF (Costco tax) = G*0.0825
COSTCO_TAX_RATE     = 0.0825
EBAY_FEE_FLAT       = 0.30
EBAY_FEE_TAX_FACTOR = 1.08
WRITE_DELAY      = 1.1         # s between sheet writes — Sheets caps writes at 60/min
MAX_REPORT_LINES  = 15          # per section in the Telegram message
CLOSE_MATCH_JACCARD = 0.8       # token overlap for a "close" title match (link suggestions)
ALERT_REPEAT_HOURS  = 24        # an identical Telegram alert is re-sent at most this often
ALERT_STATE_PATH  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "data", ".ebay_sync_alert.json")
DIGEST_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "data", ".ebay_sync_digest.json")

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


# Fallback "not live" statuses, used only when eBay's ActiveList is unknown: ENDED = Jay ended it,
# PAUSED_OOS = hidden at quantity 0 and watched by the daily sweep.
NOT_LIVE_STATUSES = frozenset({"ENDED", "PAUSED_OOS"})


def is_live_listing(ebay_url, status="", live_ids=None) -> bool:
    """
    The ONE test for "this row has a live eBay listing that must be monitored", whatever its
    sheet status. Col Q must hold a parseable item id (extract_item_id: eBay URL or bare id). With live_ids (item ids that are
    Active on eBay with quantity > 0 — live_item_ids()) membership decides; without it (eBay not
    reachable / not fetched) the row counts as live unless its status is in NOT_LIVE_STATUSES.
    """
    item_id = extract_item_id(ebay_url)
    if not item_id:
        return False
    if live_ids is not None:
        return item_id in live_ids
    return str(status or "").strip().upper() not in NOT_LIVE_STATUSES


def live_item_ids() -> set[str] | None:
    """Item ids on eBay's ActiveList that can sell (quantity − sold > 0; a listing hidden at
    quantity 0 can't). None when the list couldn't be fetched — callers fall back to col Q +
    status (is_live_listing without live_ids). One GetMyeBaySelling call (paged)."""
    try:
        listings = fetch_active_listings()
    except Exception as e:   # contract says never raises — belt and braces
        logger.warning(f"live_item_ids: ActiveList fetch failed ({e})")
        return None
    if not listings and get_last_error().get("kind"):
        return None
    return {l["item_id"] for l in listings
            if (l.get("quantity") or 0) - (l.get("quantity_sold") or 0) > 0}


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


def _headers(app_id: str, dev_id: str, cert_id: str, call_name: str = "GetMyeBaySelling") -> dict:
    return {
        "Content-Type": "text/xml; charset=utf-8",
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
        "X-EBAY-API-CALL-NAME": call_name,
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
        "listing_type":  _text(item, "ListingType"),
        "sku":           _text(item, "SKU"),
        "price":         price,
        "quantity":      _num(_text(item, "Quantity"), int, 0),
        "quantity_sold": qty_sold,
        # No tag = 0 watchers (eBay omits the field then). The field name WatchCount is
        # unconfirmed until a live listing shows >0 watchers.
        "watch_count":   _num(_text(item, "WatchCount"), int, 0),
        # GetMyeBaySelling's ActiveList sends no view count (confirmed from raw XML), so
        # this is None — "unknown", not 0. HitCount/ViewCount are still read in case eBay
        # ever sends one; nothing in the report or Telegram output uses it.
        "view_count":    _num(_text(item, "HitCount") or _text(item, "ViewCount"), int, None),
    }


def fetch_active_listings() -> list[dict]:
    """
    Fetch every active eBay listing (paged, 200/page).

    Returns [{item_id, title, price, quantity, quantity_sold, watch_count,
    view_count}, ...] (view_count is None when eBay sends none). NEVER raises: on any failure — missing credentials, network
    error, API Ack=Failure, bad XML — it logs and returns [], and sets
    `last_error` so the caller can tell that apart from "no listings". A failure
    on a later page discards earlier pages too: a partial list would make every
    unfetched listing look "removed from eBay".
    """
    _set_error(None)
    try:
        token, app_id, dev_id, cert_id, err_kind, err_msg = _load_credentials()
        if err_kind:
            msg = f"ebay_sync skipped — {err_msg}"
            logger.warning(msg)
            _set_error(err_kind, msg)
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


def _load_credentials():
    """(token, app_id, dev_id, cert_id, error_kind, error_msg) from .env.
    error_kind is None when everything is present, else "no_token" / "no_credentials"."""
    token   = os.getenv("EBAY_AUTH_TOKEN", "").strip()
    app_id  = os.getenv("EBAY_APP_ID", "").strip()
    dev_id  = os.getenv("EBAY_DEV_ID", "").strip()
    cert_id = os.getenv("EBAY_CERT_ID", "").strip()
    if not token:
        return token, app_id, dev_id, cert_id, "no_token", "no EBAY_AUTH_TOKEN"
    missing = [n for n, v in (("EBAY_APP_ID", app_id), ("EBAY_DEV_ID", dev_id),
                              ("EBAY_CERT_ID", cert_id)) if not v]
    if missing:
        return token, app_id, dev_id, cert_id, "no_credentials", f"missing {', '.join(missing)}"
    return token, app_id, dev_id, cert_id, None, ""


# ── Trading API writes (one-tap reprice) ──────────────────────────────────────

def _build_revise_xml(token: str, item_id: str, price: float) -> str:
    """ReviseFixedPriceItem body that changes ONLY the Buy It Now price (StartPrice)."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<ReviseFixedPriceItemRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        f"<Item><ItemID>{_xml_escape(str(item_id))}</ItemID>"
        f'<StartPrice currencyID="USD">{price:.2f}</StartPrice></Item>'
        "</ReviseFixedPriceItemRequest>"
    )


def _build_get_item_xml(token: str, item_id: str) -> str:
    selectors = "".join(f"<OutputSelector>{s}</OutputSelector>" for s in (
        "Item.ItemID", "Item.ListingType", "Item.SellingStatus", "Item.Title",
        "Item.Quantity", "Item.ListingDuration", "Item.OutOfStockControl", "Item.Variations",
        "Item.PictureDetails", "Item.SKU", "Item.HitCount", "Item.WatchCount",
    ))
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<GetItemRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        f"<ItemID>{_xml_escape(str(item_id))}</ItemID>"
        "<IncludeWatchCount>true</IncludeWatchCount>"   # WatchCount is only sent when asked for
        f"{selectors}"
        "</GetItemRequest>"
    )


def _call(call_name: str, body_fn) -> tuple:
    """
    Shared POST + Ack handling for single-item calls. Returns (root, error_dict):
    root is the parsed XML on success (Ack Success/Warning), else None and error_dict
    {error_kind, error_code, message}. Never raises.
    """
    token, app_id, dev_id, cert_id, err_kind, err_msg = _load_credentials()
    if err_kind:
        return None, {"error_kind": err_kind, "error_code": "", "message": err_msg}
    try:
        raw = _post(body_fn(token), _headers(app_id, dev_id, cert_id, call_name))
    except OSError as e:
        return None, {"error_kind": "network", "error_code": "", "message": f"eBay request failed: {e}"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return None, {"error_kind": "api", "error_code": "", "message": f"unparseable eBay response: {e}"}
    ack = _text(root, "Ack")
    if ack in ("Success", "Warning"):
        return root, None
    errors = _parse_errors(root)
    codes = [c for c, _ in errors]
    auth = next((c for c in codes if c in AUTH_ERROR_CODES), None)
    return None, {
        "error_kind": "auth" if auth else "api",
        "error_code": auth or (codes[0] if codes else ""),
        "message": "; ".join(f"{c}: {m}" for c, m in errors) or f"Ack={ack or 'missing'}",
    }


def revise_fixed_price(item_id, new_price) -> dict:
    """
    Change a live listing's Buy It Now price via ReviseFixedPriceItem.

    Returns {ok, item_id, price, error_kind, error_code, message}. error_kind:
    None | "invalid" (bad item ID / price — nothing was sent) | "no_token" |
    "no_credentials" | "auth" (931/932/16110 — token rejected) | "api" | "network".
    Never raises. Retrying is safe: revising to the same price is idempotent.
    """
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "price": None,
           "error_kind": None, "error_code": "", "message": ""}
    try:
        price = round(float(new_price), 2)
    except (TypeError, ValueError):
        price = None
    if not iid or price is None or price <= 0:
        out.update(error_kind="invalid",
                   message=f"refused — item_id={item_id!r} price={new_price!r}")
        logger.error(f"revise_fixed_price: {out['message']}")
        return out
    out["price"] = price
    try:
        root, err = _call("ReviseFixedPriceItem", lambda tok: _build_revise_xml(tok, iid, price))
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err:
        out.update(err)
        level = logger.critical if err["error_kind"] == "auth" else logger.error
        level(f"revise_fixed_price: item {iid} -> ${price:.2f} FAILED "
              f"[{err['error_kind']} {err['error_code']}] {err['message']}")
        return out
    out["ok"] = True
    warnings = [f"{_text(e, 'ErrorCode')}: {_text(e, 'ShortMessage')}"
                for e in root.findall("{*}Errors") if _text(e, "SeverityCode") == "Warning"]
    out["message"] = "; ".join(warnings)
    logger.info(f"revise_fixed_price: item {iid} -> ${price:.2f} OK"
                + (f" (warnings: {out['message']})" if warnings else ""))
    return out


MAX_LISTING_PICTURES = 24         # eBay's per-listing picture cap


def is_eps_url(url) -> bool:
    """True for an eBay Picture Services URL (i.ebayimg.com) — eBay forbids mixing these with
    self-hosted pictures in one listing (error 20004)."""
    m = re.match(r"^https?://([^/]+)/", str(url or "").strip())
    return bool(m) and (m.group(1) == "ebayimg.com" or m.group(1).endswith(".ebayimg.com"))


def _build_picture_xml(token: str, item_id: str, urls: list) -> str:
    """ReviseFixedPriceItem body that replaces ONLY Item.PictureDetails.PictureURL (in order)."""
    pics = "".join(f"<PictureURL>{_xml_escape(u)}</PictureURL>" for u in urls)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<ReviseFixedPriceItemRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        f"<Item><ItemID>{_xml_escape(str(item_id))}</ItemID>"
        f"<PictureDetails>{pics}</PictureDetails></Item>"
        "</ReviseFixedPriceItemRequest>"
    )


def revise_picture(item_id, main_url, gallery_urls=(), listing_type=None) -> dict:
    """
    Replace a live listing's pictures via ReviseFixedPriceItem: PictureURL = [main_url] +
    gallery_urls (deduped, capped at 24). The set is REPLACED, so the caller must pass the
    listing's existing gallery to keep it. Every URL must be EPS-hosted (i.ebayimg.com) —
    eBay rejects a self-hosted/EPS mix (20004). listing_type (from GetItem), when given, must be
    "FixedPriceItem". Same return shape as revise_fixed_price, plus "pictures" (the URLs sent).
    Never raises; nothing is sent when a guard refuses (error_kind "invalid").
    """
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "price": None, "pictures": [],
           "error_kind": None, "error_code": "", "message": ""}
    urls = []
    for u in [main_url, *(gallery_urls or [])]:
        u = str(u or "").strip()
        if u and u not in urls:
            urls.append(u)
    urls = urls[:MAX_LISTING_PICTURES]
    why = None
    if not iid:
        why = f"bad item id {item_id!r}"
    elif listing_type is not None and listing_type != "FixedPriceItem":
        why = f"listing type {listing_type or 'unknown'!r} is not FixedPriceItem"
    elif not main_url or not is_eps_url(main_url):
        why = f"main picture must be EPS-hosted, got {str(main_url)[:80]!r}"
    elif any(not is_eps_url(u) for u in urls):
        why = "gallery has a non-EPS picture (eBay error 20004 forbids mixing)"
    if why:
        out.update(error_kind="invalid", message=f"refused — {why}")
        logger.error(f"revise_picture: item {out['item_id']} {out['message']}")
        return out
    out["pictures"] = urls
    try:
        root, err = _call("ReviseFixedPriceItem", lambda tok: _build_picture_xml(tok, iid, urls))
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err:
        out.update(err)
        level = logger.critical if err["error_kind"] == "auth" else logger.error
        level(f"revise_picture: item {iid} FAILED [{err['error_kind']} {err['error_code']}] {err['message']}")
        return out
    out["ok"] = True
    warnings = [f"{_text(e, 'ErrorCode')}: {_text(e, 'ShortMessage')}"
                for e in root.findall("{*}Errors") if _text(e, "SeverityCode") == "Warning"]
    out["message"] = "; ".join(warnings)
    logger.info(f"revise_picture: item {iid} -> {len(urls)} picture(s) OK"
                + (f" (warnings: {out['message']})" if warnings else ""))
    return out


def _cdata(text: str) -> str:
    """Wrap text in CDATA (splitting any ']]>' so it can't close the section early)."""
    return "<![CDATA[" + str(text).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _build_description_xml(token: str, item_id: str, description: str) -> str:
    """ReviseFixedPriceItem body that replaces ONLY Item.Description."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<ReviseFixedPriceItemRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        f"<Item><ItemID>{_xml_escape(str(item_id))}</ItemID>"
        f"<Description>{_cdata(description)}</Description></Item>"
        "</ReviseFixedPriceItemRequest>"
    )


def revise_description(item_id, new_description, listing_type=None) -> dict:
    """
    Replace a live listing's HTML description via ReviseFixedPriceItem (in place, no re-list).
    listing_type (from GetItem / ActiveList), when given, must be "FixedPriceItem". Same return
    shape as revise_fixed_price. Never raises; a refused guard sends nothing (error_kind "invalid").
    """
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "price": None,
           "error_kind": None, "error_code": "", "message": ""}
    desc = str(new_description or "").strip()
    why = None
    if not iid:
        why = f"bad item id {item_id!r}"
    elif listing_type is not None and listing_type != "FixedPriceItem":
        why = f"listing type {listing_type or 'unknown'!r} is not FixedPriceItem"
    elif not desc:
        why = "empty description"
    if why:
        out.update(error_kind="invalid", message=f"refused — {why}")
        logger.error(f"revise_description: item {out['item_id']} {out['message']}")
        return out
    try:
        root, err = _call("ReviseFixedPriceItem", lambda tok: _build_description_xml(tok, iid, desc))
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err:
        out.update(err)
        level = logger.critical if err["error_kind"] == "auth" else logger.error
        level(f"revise_description: item {iid} FAILED [{err['error_kind']} {err['error_code']}] {err['message']}")
        return out
    out["ok"] = True
    warnings = [f"{_text(e, 'ErrorCode')}: {_text(e, 'ShortMessage')}"
                for e in root.findall("{*}Errors") if _text(e, "SeverityCode") == "Warning"]
    out["message"] = "; ".join(warnings)
    logger.info(f"revise_description: item {iid} OK" + (f" (warnings: {out['message']})" if warnings else ""))
    return out


def _build_sku_xml(token: str, item_id: str, sku: str) -> str:
    """ReviseFixedPriceItem body that changes ONLY Item.SKU."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<ReviseFixedPriceItemRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        f"<Item><ItemID>{_xml_escape(str(item_id))}</ItemID>"
        f"<SKU>{_xml_escape(sku)}</SKU></Item>"
        "</ReviseFixedPriceItemRequest>"
    )


MAX_SKU_LEN = 50   # eBay's SKU field limit


def revise_sku(item_id, sku, listing_type=None) -> dict:
    """
    Set a live listing's SKU via ReviseFixedPriceItem (in place, no re-list). listing_type, when
    given, must be "FixedPriceItem". Same return shape as revise_fixed_price. Never raises; a
    refused guard sends nothing (error_kind "invalid").
    """
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "price": None,
           "error_kind": None, "error_code": "", "message": ""}
    sku = str(sku or "").strip()
    why = None
    if not iid:
        why = f"bad item id {item_id!r}"
    elif listing_type is not None and listing_type != "FixedPriceItem":
        why = f"listing type {listing_type or 'unknown'!r} is not FixedPriceItem"
    elif not sku or len(sku) > MAX_SKU_LEN:
        why = f"bad SKU {sku!r}"
    if why:
        out.update(error_kind="invalid", message=f"refused — {why}")
        logger.error(f"revise_sku: item {out['item_id']} {out['message']}")
        return out
    try:
        root, err = _call("ReviseFixedPriceItem", lambda tok: _build_sku_xml(tok, iid, sku))
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err:
        out.update(err)
        level = logger.critical if err["error_kind"] == "auth" else logger.error
        level(f"revise_sku: item {iid} -> {sku} FAILED [{err['error_kind']} {err['error_code']}] {err['message']}")
        return out
    out["ok"] = True
    warnings = [f"{_text(e, 'ErrorCode')}: {_text(e, 'ShortMessage')}"
                for e in root.findall("{*}Errors") if _text(e, "SeverityCode") == "Warning"]
    out["message"] = "; ".join(warnings)
    logger.info(f"revise_sku: item {iid} -> {sku} OK" + (f" (warnings: {out['message']})" if warnings else ""))
    return out


def _build_title_xml(token: str, item_id: str, title: str) -> str:
    """ReviseFixedPriceItem body that changes ONLY Item.Title."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<ReviseFixedPriceItemRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        f"<Item><ItemID>{_xml_escape(str(item_id))}</ItemID>"
        f"<Title>{_xml_escape(title)}</Title></Item>"
        "</ReviseFixedPriceItemRequest>"
    )


MAX_TITLE_LEN = 80   # eBay's title limit


def revise_title(item_id, title, listing_type=None) -> dict:
    """
    Set a live listing's title via ReviseFixedPriceItem (in place, no re-list). listing_type, when
    given, must be "FixedPriceItem". Same return shape as revise_fixed_price. Never raises; a
    refused guard sends nothing (error_kind "invalid").
    """
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "price": None,
           "error_kind": None, "error_code": "", "message": ""}
    title = " ".join(str(title or "").split())
    why = None
    if not iid:
        why = f"bad item id {item_id!r}"
    elif listing_type is not None and listing_type != "FixedPriceItem":
        why = f"listing type {listing_type or 'unknown'!r} is not FixedPriceItem"
    elif not title or len(title) > MAX_TITLE_LEN:
        why = f"bad title {title!r} ({len(title)} chars, max {MAX_TITLE_LEN})"
    if why:
        out.update(error_kind="invalid", message=f"refused — {why}")
        logger.error(f"revise_title: item {out['item_id']} {out['message']}")
        return out
    try:
        root, err = _call("ReviseFixedPriceItem", lambda tok: _build_title_xml(tok, iid, title))
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err:
        out.update(err)
        level = logger.critical if err["error_kind"] == "auth" else logger.error
        level(f"revise_title: item {iid} FAILED [{err['error_kind']} {err['error_code']}] {err['message']}")
        return out
    out["ok"] = True
    warnings = [f"{_text(e, 'ErrorCode')}: {_text(e, 'ShortMessage')}"
                for e in root.findall("{*}Errors") if _text(e, "SeverityCode") == "Warning"]
    out["message"] = "; ".join(warnings)
    logger.info(f"revise_title: item {iid} -> {title!r} OK" + (f" (warnings: {out['message']})" if warnings else ""))
    return out


def fetch_item_description(item_id) -> dict:
    """GetItem (DetailLevel ReturnAll) -> {ok, item_id, description, listing_type, listing_status,
    has_variations, error_kind, error_code, message}. Never raises."""
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "description": "", "listing_type": "",
           "listing_status": "", "has_variations": False,
           "error_kind": None, "error_code": "", "message": ""}
    if not iid:
        out.update(error_kind="invalid", message=f"bad item id {item_id!r}")
        return out
    selectors = "".join(f"<OutputSelector>{s}</OutputSelector>" for s in (
        "Item.ItemID", "Item.Description", "Item.ListingType", "Item.SellingStatus", "Item.Variations"))

    def body(tok):
        return ('<?xml version="1.0" encoding="utf-8"?>'
                f'<GetItemRequest xmlns="{_NS}">'
                f"<RequesterCredentials><eBayAuthToken>{_xml_escape(tok)}</eBayAuthToken></RequesterCredentials>"
                "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
                f"<ItemID>{_xml_escape(iid)}</ItemID><DetailLevel>ReturnAll</DetailLevel>{selectors}"
                "</GetItemRequest>")
    try:
        root, err = _call("GetItem", body)
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err:
        out.update(err)
        return out
    item = _child(root, "Item")
    if item is None:
        out.update(error_kind="api", message="GetItem returned no Item")
        return out
    out["description"] = _text(item, "Description")
    out["listing_type"] = _text(item, "ListingType")
    status = _child(item, "SellingStatus")
    out["listing_status"] = _text(status, "ListingStatus") if status is not None else ""
    variations = _child(item, "Variations")
    out["has_variations"] = variations is not None and variations.find("{*}Variation") is not None
    out["ok"] = True
    return out


ALREADY_ENDED_CODES = {"1047"}    # "The auction has already been closed."


def _build_end_xml(token: str, item_id: str, reason: str = "NotAvailable") -> str:
    """EndItem body. reason: eBay EndReasonCodeType — NotAvailable = item no longer available."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<EndItemRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        f"<ItemID>{_xml_escape(str(item_id))}</ItemID>"
        f"<EndingReason>{_xml_escape(reason)}</EndingReason>"
        "</EndItemRequest>"
    )


def end_fixed_price(item_id, reason="NotAvailable") -> dict:
    """
    End a live listing via EndItem. Same return shape as revise_fixed_price:
    {ok, item_id, price (always None), error_kind, error_code, message}. Never raises.
    Currently UNUSED (2026-10-05): OOS and the scheduled sale-end action both hide the listing
    with set_quantity_zero instead, which keeps watchers and search rank. Kept for a future
    explicit "end for good" action.
    Error 1047 (already ended) counts as ok — e.g. a retried call whose first attempt landed.
    """
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "price": None,
           "error_kind": None, "error_code": "", "message": ""}
    if not iid:
        out.update(error_kind="invalid", message=f"refused — item_id={item_id!r}")
        logger.error(f"end_fixed_price: {out['message']}")
        return out
    try:
        root, err = _call("EndItem", lambda tok: _build_end_xml(tok, iid, reason))
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err and err["error_code"] in ALREADY_ENDED_CODES:
        out.update(ok=True, message=f"already ended ({err['message']})")
        logger.info(f"end_fixed_price: item {iid} was already ended")
        return out
    if err:
        out.update(err)
        level = logger.critical if err["error_kind"] == "auth" else logger.error
        level(f"end_fixed_price: item {iid} FAILED [{err['error_kind']} {err['error_code']}] {err['message']}")
        return out
    out["ok"] = True
    out["message"] = f"ended {_text(root, 'EndTime')}".strip()
    logger.info(f"end_fixed_price: item {iid} ended")
    return out


def _build_qty_xml(token: str, item_id: str, qty: int) -> str:
    """ReviseInventoryStatus body — quantity only (a multi-variation listing needs a SKU and
    is rejected by eBay; the error surfaces to the caller)."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<ReviseInventoryStatusRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        "<InventoryStatus>"
        f"<ItemID>{_xml_escape(str(item_id))}</ItemID>"
        f"<Quantity>{int(qty)}</Quantity>"
        "</InventoryStatus>"
        "</ReviseInventoryStatusRequest>"
    )


def set_quantity(item_id, qty) -> dict:
    """
    Set a live listing's available quantity via ReviseInventoryStatus. qty 0 + the account's
    Out of Stock setting + a GTC listing = listing HIDDEN (watchers, sales history and search
    rank kept); restoring qty > 0 brings it back. Without the setting, or on a fixed-duration
    listing, eBay ENDS a qty-0 listing. Same return shape as end_fixed_price:
    {ok, item_id, price (always None), error_kind, error_code, message}. Never raises.
    Idempotent — setting the same quantity twice is safe.
    """
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "price": None,
           "error_kind": None, "error_code": "", "message": ""}
    try:
        q = int(qty)
        if q != float(qty):
            q = None
    except (TypeError, ValueError):
        q = None
    if not iid or q is None or q < 0:
        out.update(error_kind="invalid", message=f"refused — item_id={item_id!r} qty={qty!r}")
        logger.error(f"set_quantity: {out['message']}")
        return out
    try:
        root, err = _call("ReviseInventoryStatus", lambda tok: _build_qty_xml(tok, iid, q))
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err:
        out.update(err)
        level = logger.critical if err["error_kind"] == "auth" else logger.error
        level(f"set_quantity: item {iid} -> {q} FAILED [{err['error_kind']} {err['error_code']}] {err['message']}")
        return out
    out["ok"] = True
    out["message"] = f"quantity {q}"
    logger.info(f"set_quantity: item {iid} quantity -> {q}")
    return out


def set_quantity_zero(item_id) -> dict:
    """Hide a live listing (quantity 0) — see set_quantity."""
    return set_quantity(item_id, 0)


def fetch_item_price(item_id) -> dict:
    """
    Live Buy It Now price of one listing via GetItem — the reprice guard reads this rather
    than col H, which Jay edits by hand and can be stale.
    Returns {ok, item_id, price, listing_status, listing_type, quantity_available,
    listing_duration, out_of_stock_control, has_variations, picture_urls, sku, view_count,
    watch_count, error_kind, error_code, message}. view_count = HitCount (None = unknown);
    watch_count = WatchCount (absent tag = 0 watchers, like ActiveList; None on a failed call).
    quantity_available = Quantity - QuantitySold (None if unknown); listing_duration e.g. "GTC";
    out_of_stock_control True/False, None when eBay didn't send it; has_variations True for a
    multi-variation listing (ReviseFixedPriceItem can't set its price without a SKU). Never raises.
    """
    iid = extract_item_id(item_id)
    out = {"ok": False, "item_id": iid or str(item_id or ""), "price": None,
           "listing_status": "", "listing_type": "",
           "quantity_available": None, "listing_duration": "", "out_of_stock_control": None,
           "has_variations": False, "picture_urls": [], "sku": "",
           "view_count": None, "watch_count": None,
           "error_kind": None, "error_code": "", "message": ""}
    if not iid:
        out.update(error_kind="invalid", message=f"bad item id {item_id!r}")
        return out
    try:
        root, err = _call("GetItem", lambda tok: _build_get_item_xml(tok, iid))
    except Exception as e:  # contract: never raises
        root, err = None, {"error_kind": "api", "error_code": "", "message": f"unexpected error: {e}"}
    if err:
        out.update(err)
        logger.error(f"fetch_item_price: item {iid} [{err['error_kind']} {err['error_code']}] {err['message']}")
        return out
    item = _child(root, "Item")
    status = _child(item, "SellingStatus") if item is not None else None
    if status is not None:
        out["price"] = _num(_text(status, "CurrentPrice"), float, None)
        out["listing_status"] = _text(status, "ListingStatus")
    if item is not None:
        out["listing_type"] = _text(item, "ListingType")
        out["sku"] = _text(item, "SKU")
        out["listing_duration"] = _text(item, "ListingDuration")
        out["view_count"] = _num(_text(item, "HitCount"), int, None)
        out["watch_count"] = _num(_text(item, "WatchCount"), int, 0)
        oosc = _text(item, "OutOfStockControl").lower()
        out["out_of_stock_control"] = {"true": True, "false": False}.get(oosc)
        variations = _child(item, "Variations")
        out["has_variations"] = (variations is not None
                                 and variations.find("{*}Variation") is not None)
        pics = _child(item, "PictureDetails")
        out["picture_urls"] = ([(e.text or "").strip() for e in pics.findall("{*}PictureURL")
                                if (e.text or "").strip()] if pics is not None else [])
        total = _num(_text(item, "Quantity"), int, None)
        sold = _num(_text(status, "QuantitySold"), int, 0) if status is not None else 0
        if total is not None:
            out["quantity_available"] = max(total - (sold or 0), 0)
    out["ok"] = out["price"] is not None
    if not out["ok"]:
        out.update(error_kind="api", message="GetItem returned no CurrentPrice")
    return out


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


def _to_rate(value):
    """Fee rate cell -> fraction. '0.1325', '13.25%' and 13.25 (>1 = percent) all -> 0.1325;
    blank/garbage -> None (unknown, never assumed 0)."""
    s = str(value).strip()
    pct = s.endswith("%")
    f = _to_float(s.rstrip("%"))
    if f is None or f < 0:
        return None
    return f / 100 if (pct or f > 1) else f


def _cost_basis(buy_cost, costco_cost):
    """costco_cost (col G — current Costco price, what a drop-shipper pays at sale time)
    if present, parseable and > 0, else buy_cost (col BB legacy snapshot) as fallback."""
    g = _to_float(costco_cost)
    if g is not None and g > 0:
        return g
    return _to_float(buy_cost)


def compute_net(ebay_price, cost_basis, fee_rate, ship=0.0, ad=0.0) -> float:
    """Mirrors the sheet's net_profit (I = H - G - AC - AD - AE - AF) with the live eBay
    price for H and cost_basis for G. AC = H*AB*1.08 + 0.30 (FVF on item + ~8% avg buyer
    tax + $0.30/order); AF = G*0.0825 (Jay's Costco sales tax)."""
    fee = ebay_price * fee_rate * EBAY_FEE_TAX_FACTOR + EBAY_FEE_FLAT
    tax = cost_basis * COSTCO_TAX_RATE
    return ebay_price - cost_basis - fee - (ship or 0.0) - (ad or 0.0) - tax


def margin_flag(ebay_price, cost_basis, fee_rate, ship, ad, sold_90d, *,
                floor_price=FLOOR_PRICE, floor_margin_pct=FLOOR_MARGIN_PCT):
    """
    "hard" (net < 0) | "floor" (price >= floor_price AND margin < floor_margin_pct) |
    "soft" (0 <= net < MARGIN_SOFT_FLOOR and sold_90d == 0) | None.

    Priority is hard > floor > soft, so an expensive item running thin flags as "floor"
    (Jay reprices it up) rather than disappearing into the soft digest. Unknown
    price / cost / fee rate -> None (can't judge; never assume $0). Missing ship/ad
    count as 0. sold_90d None (blank) is unknown, NOT 0, so a thin-margin row with no
    velocity data stays silent; high-velocity items ride at $3.99 silently.
    """
    if ebay_price is None or cost_basis is None or fee_rate is None:
        return None
    net = round(compute_net(ebay_price, cost_basis, fee_rate, ship, ad), 2)
    if net < 0:
        return "hard"
    if ebay_price >= floor_price and ebay_price > 0 and net / ebay_price < floor_margin_pct:
        return "floor"
    if net < MARGIN_SOFT_FLOOR and sold_90d == 0:
        return "soft"
    return None


def _break_even(cost_basis, fee_rate, ship, ad):
    """Lowest price at which net >= 0 (matches compute_net: Costco tax + $0.30 + buyer-tax
    factor). Solving net = 0 for H gives H = (G*(1+tax) + 0.30 + ship + ad) / (1 - AB*1.08)."""
    denom = 1 - fee_rate * EBAY_FEE_TAX_FACTOR
    if fee_rate >= 1 or denom <= 0:
        return None
    return (cost_basis * (1 + COSTCO_TAX_RATE) + EBAY_FEE_FLAT + (ship or 0.0) + (ad or 0.0)) / denom


MARGIN_COLS = ("buy_cost", "costco_cost", "fee_rate", "ship_cost", "ad_cost", "sold_90d")
# sheet-driven reprice inputs (col BD target_price, col BE manual_cost)
TARGET_COLS = ("target_price", "manual_cost")
_IDENTITY_COLS = ("status", "title", "platform", "ebay_price", "ebay_listing_url", "units_sold")
READ_LAST_COL = "BE"     # load_sheet_rows / _read_row range end — keep >= every column read here


def _row_index(COL) -> dict:
    """{field: 0-based index} for every column load_sheet_rows reads (tolerant of an older col_map)."""
    idx = {k: _col_idx(COL[k]) for k in _IDENTITY_COLS}
    idx.update({k: _col_idx(COL[k]) for k in MARGIN_COLS + TARGET_COLS if k in COL})
    return idx


def load_sheet_rows(service, COL, sheet_name, start_row, end_row) -> list[dict]:
    """Read the tracker into dicts (row_num = absolute sheet row). Skips empty rows."""
    raw = read_sheet(service, f"'{sheet_name}'!A{start_row}:{READ_LAST_COL}{end_row}")
    idx = _row_index(COL)
    rows = []
    for offset, r in enumerate(raw):
        row = {k: _cell(r, i) for k, i in idx.items()}
        # emptiness judged on the identity columns only — margin inputs can be formula noise
        if not any(str(row[k]).strip() for k in _IDENTITY_COLS):
            continue
        row["row_num"] = start_row + offset
        rows.append(row)
    return rows


def _read_row(service, COL, sheet_name, row_num) -> dict | None:
    """One row re-read fresh (same keys as load_sheet_rows) — the pre-write guard of the
    target-price sweep. None when the row is empty/unreadable."""
    raw = read_sheet(service, f"'{sheet_name}'!A{row_num}:{READ_LAST_COL}{row_num}")
    if not raw:
        return None
    return {k: _cell(raw[0], i) for k, i in _row_index(COL).items()}


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

def _norm_title(s) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed — for title comparison."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s).lower()).split())


def token_jaccard(norm_a: str, norm_b: str) -> float:
    """Jaccard similarity of two already-normalised titles' word sets (0.0 when both empty)."""
    a, b = set(norm_a.split()), set(norm_b.split())
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _suggest_links(entries, pool) -> dict:
    """
    Suggest an eBay item for each unlinked ACTIVE row. entries: dicts with row_num +
    title; pool: eBay listings no sheet row points at. Returns
    {row_num: (listing, "exact" | "close")}.

    exact = equal normalised titles; close = token Jaccard >= CLOSE_MATCH_JACCARD.
    Never guesses: a row whose best score is shared by several listings, or a listing
    wanted by several rows, gets no suggestion.
    """
    pool_norm = [(l, _norm_title(l["title"])) for l in pool]
    picks = {}                                   # row_num -> (listing, kind)
    for e in entries:
        norm = _norm_title(e["title"])
        if not norm:
            continue
        best_score, best = None, []
        for l, ln in pool_norm:
            if ln == norm:
                score, kind = (1, 1.0), "exact"
            else:
                jac = token_jaccard(norm, ln)
                if jac < CLOSE_MATCH_JACCARD:
                    continue
                score, kind = (0, jac), "close"
            if best_score is None or score > best_score:
                best_score, best = score, [(l, kind)]
            elif score == best_score:
                best.append((l, kind))
        if len(best) == 1:
            picks[e["row_num"]] = best[0]

    wanted = {}
    for row_num, (l, _) in picks.items():
        wanted.setdefault(l["item_id"], []).append(row_num)
    return {r: v for r, v in picks.items() if len(wanted[v[0]["item_id"]]) == 1}


def _check_margin(row, listing, report, floor_price=FLOOR_PRICE, floor_margin_pct=FLOOR_MARGIN_PCT):
    """Evaluate one matched row's margin at the LIVE eBay price; append to
    report["margin_breach"] (hard/floor/soft) or report["margin_unchecked"] (inputs unknown)."""
    price = listing["price"]
    cost = _cost_basis(row.get("buy_cost"), row.get("costco_cost"))
    fee = _to_rate(row.get("fee_rate", ""))
    ship = _to_float(row.get("ship_cost", "")) or 0.0
    ad = _to_float(row.get("ad_cost", "")) or 0.0
    sold_raw = _to_float(row.get("sold_90d", ""))
    sold = None if sold_raw is None else int(sold_raw)
    base = {"row_num": row["row_num"], "title": row.get("title", ""),
            "item_id": listing["item_id"],
            "status": str(row.get("status", "")).strip().upper(),
            # manual-wins re-arm key for auto_reprice_breaches (col BE vs col G)
            "manual_cost": row.get("manual_cost", ""), "costco_cost": row.get("costco_cost", "")}

    severity = margin_flag(price, cost, fee, ship, ad, sold,
                           floor_price=floor_price, floor_margin_pct=floor_margin_pct)
    if price is None or cost is None or fee is None:
        # what's missing + whether it can sell (alert_message alerts only those that can)
        missing = [label for val, label in ((cost, "col G (cost)"), (fee, "col AB (fee rate)"),
                                            (price, "live eBay price")) if val is None]
        available = (listing.get("quantity") or 0) - (listing.get("quantity_sold") or 0)
        report["margin_unchecked"].append({**base, "missing": missing,
                                           "can_sell": available > 0})
    elif severity:
        net = round(compute_net(price, cost, fee, ship, ad), 2)
        report["margin_breach"].append({
            **base, "severity": severity, "ebay_price": price, "cost_basis": cost,
            "net": net, "margin": round(net / price * 100, 1) if price else None,
            "break_even": _break_even(cost, fee, ship, ad)})


def sync(service, sheet_rows, listings, *, dry_run=False, sheet_name=None,
         col_map=None, title_reader=None, cancelled_units=None, floor_price=FLOOR_PRICE,
         floor_margin_pct=FLOOR_MARGIN_PCT) -> dict:
    """
    Match listings to sheet rows by the item ID in col Q. Flag-only except two writes:
    units_sold (col U) when QuantitySold differs, and buy_cost (col BB) snapshotted
    from costco_cost when a NEW sale lands on a row whose buy_cost is still blank.
    (Col H is mirrored afterwards by mirror_live_prices, from report["matched"].)

    sheet_rows:    dicts from load_sheet_rows (row_num, title, status, platform,
                   ebay_price, ebay_listing_url, units_sold, plus the margin inputs
                   buy_cost, costco_cost, fee_rate, ship_cost, ad_cost, sold_90d —
                   any may be absent/blank)
    title_reader:  optional callable -> {row_num: title}; called once before the
                   first write. A row whose title no longer matches (auditor deleted
                   rows since the read) is skipped, not written.
    cancelled_units: {item_id: units} in cancelled/refunded orders (default: read from the
                   sales log). Col U = eBay QuantitySold minus these, floored at 0 — eBay's
                   count still includes a refunded-before-shipping order. Only items the log
                   knows about are adjusted; no history = raw QuantitySold.

    Returns {matched, updated, margin_breach, margin_unchecked, on_ebay_not_in_sheet,
             active_not_on_ebay, duplicate_url, stale_rows, write_errors, dry_run}.
    `matched` = every matched row; `updated` = the subset whose col U changed.
    `margin_breach` = [{..., severity: "hard"|"soft", net, break_even}] for matched rows;
    `margin_unchecked` = matched rows whose cost / fee rate / live price is unknown.
    """
    if sheet_name is None or col_map is None:
        d_sheet, d_cols = _load_defaults()
        sheet_name = sheet_name or d_sheet
        col_map = col_map or d_cols
    units_col = col_map["units_sold"]

    report = {"matched": [], "updated": [], "margin_breach": [], "margin_unchecked": [],
              "on_ebay_not_in_sheet": [], "active_not_on_ebay": [],
              "duplicate_url": [], "stale_rows": [], "write_errors": [],
              "buy_cost_snapshots": [], "dry_run": dry_run,
              "floor_price": floor_price, "floor_margin_pct": floor_margin_pct}

    if cancelled_units is None:
        try:
            from tools import sales_log
            cancelled_units = sales_log.cancelled_units(sales_log.load())
        except Exception as e:   # never let the log break the units_sold sync
            logger.warning(f"ebay_sync: sales log unavailable for col U ({e}) — raw QuantitySold")
            cancelled_units = {}

    by_id = {l["item_id"]: l for l in listings}
    claimed = set()
    to_write = []
    unlinked = []          # ACTIVE rows with a blank col Q (candidates for a link suggestion)

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
            cancelled = int(cancelled_units.get(item_id, 0) or 0)
            new_sold = max(listing["quantity_sold"] - cancelled, 0)
            entry = {
                "row_num": row["row_num"], "title": title, "item_id": item_id,
                "old_units_sold": old_sold, "quantity_sold": new_sold,
                "cancelled_units": cancelled,
                "status": str(row.get("status", "")).strip().upper(),
                "sheet_price": _to_float(row.get("ebay_price", "")),
                "available": max(listing["quantity"] - new_sold, 0),
                "watch_count": listing["watch_count"], "price": listing["price"],
            }
            report["matched"].append(entry)
            if new_sold != old_sold:
                report["updated"].append(entry)
                to_write.append(entry)
                if new_sold < old_sold:
                    logger.warning(f"ebay_sync: row {row['row_num']} units_sold "
                                   f"{old_sold} -> {new_sold} (eBay count went down)")
                # Auto-snapshot buy_cost (col BB) on a NEW sale: quantity_sold rose and
                # buy_cost is still blank → record the current costco_cost as what Jay is
                # about to pay. Manual override always wins (a filled buy_cost is never
                # touched).
                if new_sold > old_sold:
                    _bc = _to_float(row.get("buy_cost"))
                    _gc = _to_float(row.get("costco_cost"))
                    if (_bc is None or _bc <= 0) and _gc is not None and _gc > 0:
                        entry["buy_cost_snapshot"] = row.get("costco_cost")
                        report["buy_cost_snapshots"].append(
                            {"row_num": row["row_num"], "title": title,
                             "buy_cost": row.get("costco_cost")})

            _check_margin(row, listing, report, floor_price, floor_margin_pct)
        elif (str(row.get("status", "")).strip().upper() == "ACTIVE"
              and str(row.get("platform", "")).strip().lower() in ("", "ebay", "both")):
            blank_url = False
            if item_id:
                reason = "not in eBay active listings (sold out / removed)"
            elif str(row.get("ebay_listing_url", "")).strip():
                reason = "col Q has no parseable eBay item ID"
            else:
                reason = "no URL in col Q"
                blank_url = True
            entry = {"row_num": row["row_num"], "title": title, "item_id": item_id, "reason": reason}
            report["active_not_on_ebay"].append(entry)
            if blank_url:
                unlinked.append(entry)

    sheet_ids = {extract_item_id(r.get("ebay_listing_url")) for r in sheet_rows}
    report["on_ebay_not_in_sheet"] = [dict(l) for l in listings if l["item_id"] not in sheet_ids]

    # Flag-only: suggest (never write) the col Q link for unlinked ACTIVE rows.
    pool = {l["item_id"]: l for l in report["on_ebay_not_in_sheet"]}
    suggestions = _suggest_links(unlinked, list(pool.values()))
    for entry in unlinked:
        pick = suggestions.get(entry["row_num"])
        if pick is None:
            continue
        listing, kind = pick
        entry["suggested_item_id"] = listing["item_id"]
        entry["match_kind"] = kind
        entry["reason"] += (f" — likely = eBay item {listing['item_id']}, fill col Q"
                            if kind == "exact" else
                            f" — possibly = eBay item {listing['item_id']} (close title match), fill col Q")
        pool[listing["item_id"]]["suggested_row"] = entry["row_num"]
        pool[listing["item_id"]]["match_kind"] = kind     # alert_message's orphan hint

    if to_write and not dry_run:
        _write_units_sold(service, sheet_name, units_col, to_write, report, title_reader,
                          buy_cost_col=col_map.get("buy_cost"))
    return report


def _write_units_sold(service, sheet_name, units_col, entries, report, title_reader,
                      buy_cost_col=None):
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
        cols = [(units_col, entry["quantity_sold"])]
        if buy_cost_col and entry.get("buy_cost_snapshot"):
            cols.append((buy_cost_col, entry["buy_cost_snapshot"]))
        try:
            safe_write_row(service, sheet_name, entry["row_num"], cols)
        except Exception as e:
            logger.error(f"ebay_sync: write failed for row {entry['row_num']}: {e}")
            report["write_errors"].append(
                {"row_num": entry["row_num"], "title": entry["title"], "error": str(e)[:150]})


def summarize(report: dict) -> str:
    """One-line summary for the Run Log notes column."""
    hard = sum(1 for m in report["margin_breach"] if m["severity"] == "hard")
    soft = sum(1 for m in report["margin_breach"] if m["severity"] == "soft")
    floor = sum(1 for m in report["margin_breach"] if m["severity"] == "floor")
    s = (f"matched {len(report['matched'])}, updated {len(report['updated'])}, "
         f"margin_breach hard {hard} soft {soft} floor {floor}, "
         f"active_not_on_ebay {len(report['active_not_on_ebay'])}, "
         f"not_in_sheet {len(report['on_ebay_not_in_sheet'])}")
    if report.get("buy_cost_snapshots"):
        s += f", buy_cost_snapped {len(report['buy_cost_snapshots'])}"
    n_mirror = sum(1 for m in report.get("h_mirror") or [] if m["outcome"] in ("mirrored", "dry_run"))
    if n_mirror:
        s += f", h_mirrored {n_mirror}"
    for outcome in ("repriced", "held", "failed", "manual"):
        n = sum(1 for m in report["margin_breach"] if m.get("auto") == outcome)
        if n:
            s += f", auto_{outcome} {n}"
    if report["margin_unchecked"]:
        s += f", margin_unchecked {len(report['margin_unchecked'])}"
    n_links = sum(1 for g in report["active_not_on_ebay"] if g.get("suggested_item_id"))
    if n_links:
        s += f", link_suggestions {n_links}"
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
    Telegram HTML message, or None when there is nothing to flag. HARD margin breaches
    (net < 0), FLOOR breaches (expensive item below the margin floor) and
    ACTIVE-but-not-on-eBay rows warrant a message; SOFT breaches go to digest_message(),
    everything else stays in the Run Log. Raw price movement is never alerted.
    Sheet-derived titles are truncated THEN html-escaped.
    """
    # Breaches auto-repriced to break-even this run are fixed — they show in the
    # auto-reprice summary instead. Failed / held ones still need Jay.
    losing = [m for m in report["margin_breach"]
              if m["severity"] == "hard" and m.get("auto") != "repriced"]
    floor = [m for m in report["margin_breach"] if m["severity"] == "floor"]
    gone = report["active_not_on_ebay"]
    # Orphans at quantity 0 are hidden (or sold out) — they can't sell, so they don't alert.
    orphans = [o for o in report["on_ebay_not_in_sheet"]
               if (o.get("quantity") or 0) - (o.get("quantity_sold") or 0) > 0]
    hidden_orphans = len(report["on_ebay_not_in_sheet"]) - len(orphans)
    # A live listing with no cost / fee can't be margin-checked: it could sell at a loss
    # unseen (Grand Leisure rows 95/96, 10/21). Hidden at quantity 0 = can't sell, no alert.
    unchecked = [u for u in report.get("margin_unchecked", []) if u.get("can_sell", True)]
    if not losing and not floor and not gone and not orphans and not unchecked:
        return None
    msg = ["🛒 <b>eBay sync</b>" + (" (dry run)" if report.get("dry_run") else "")]
    if losing:
        def _line(m):
            be = f", break-even ${m['break_even']:.2f}" if m.get("break_even") else ""
            manual = (" — your manual price; auto-reprice is off until Costco's cost changes"
                      if m.get("auto") == "manual" else "")
            return (f"• losing money on {html.escape(_truncate(m['title']))} — net "
                    f"-${abs(m['net']):.2f} (eBay ${m['ebay_price']:.2f}{be}) (row {m['row_num']})"
                    f"{manual}")
        msg += _section(f"\n💸 <b>Losing money ({len(losing)})</b>", [_line(m) for m in losing])
    if floor:
        floor_pct = report.get("floor_margin_pct", FLOOR_MARGIN_PCT)
        floor_price = report.get("floor_price", FLOOR_PRICE)
        def _fline(m):
            mgn = f"{m['margin']:.1f}% margin" if m.get("margin") is not None else "?% margin"
            return (f"• {html.escape(_truncate(m['title']))} — ${m['ebay_price']:.2f} at "
                    f"{mgn} (net ${m['net']:.2f}) (row {m['row_num']})")
        msg += _section(f"\n📉 <b>Below {floor_pct:.0%} margin floor — ≥ ${floor_price:.0f} "
                        f"({len(floor)})</b>", [_fline(m) for m in floor])
    if unchecked:
        def _uline(u):
            missing = ", ".join(u.get("missing") or ["cost / fee"])
            return (f"• {html.escape(_truncate(u['title']))} — missing {missing} "
                    f"(eBay {u['item_id']}) (row {u['row_num']})")
        msg += _section(f"\n❓ <b>No margin check — live listing missing cost/fee "
                        f"({len(unchecked)})</b>", [_uline(u) for u in unchecked])
    if gone:
        msg += _section(f"\n⚠️ <b>ACTIVE but not on eBay ({len(gone)})</b>", [
            f"• {html.escape(_truncate(g['title']))} — {html.escape(g['reason'])} (row {g['row_num']})"
            for g in gone])
    if orphans:
        # Live on eBay but no sheet row watches it — nothing hides it when Costco goes OOS.
        def _oline(o):
            row = o.get("suggested_row")
            if row is None:
                hint = "no matching sheet row, end it or link it by hand"
            elif o.get("match_kind") == "close":
                hint = f"row {row}, close title match, verify then fill col Q"
            else:
                hint = f"row {row}, blank col Q, fill col Q to link"
            return f"• {html.escape(_truncate(o['title']))} — eBay {o['item_id']} ({hint})"
        msg += _section(f"\n🔗 <b>On eBay but not in the sheet ({len(orphans)})</b>",
                        [_oline(o) for o in orphans])
    if hidden_orphans:
        msg.append(f"\nℹ️ {hidden_orphans} more not in the sheet, hidden at quantity 0 (not selling)")
    return "\n".join(msg)


def digest_message(report: dict) -> str | None:
    """SOFT breaches (thin margin, no 90-day sales) as one Telegram HTML message, or None."""
    soft = [m for m in report["margin_breach"] if m["severity"] == "soft"]
    if not soft:
        return None
    msg = ["🛒 <b>eBay sync — daily thin-margin digest</b>" + (" (dry run)" if report.get("dry_run") else "")]
    msg += _section(f"\n🐢 <b>Thin margin, no recent sales ({len(soft)})</b>", [
        f"• {html.escape(_truncate(m['title']))} — net ${m['net']:.2f} at eBay "
        f"${m['ebay_price']:.2f} (row {m['row_num']})" for m in soft])
    return "\n".join(msg)


def _digest_due(dry_run=False, today=None) -> bool:
    """True at most once per calendar day. Records the day when it returns True; a dry run
    neither consumes nor records the slot. An unreadable/unwritable state file means send
    (a duplicate digest beats a silently missing one)."""
    if dry_run:
        return True
    today = today or time.strftime("%Y-%m-%d")
    try:
        with open(DIGEST_STATE_PATH, encoding="utf-8") as f:
            if json.load(f).get("date") == today:
                return False
    except (OSError, ValueError):
        pass
    try:
        with open(DIGEST_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"date": today}, f)
    except OSError as e:
        logger.warning(f"ebay_sync: digest state unavailable ({e}) — sending anyway")
    return True


def _dedupe_alert(alert, dry_run=False, now=None):
    """
    Suppress an alert identical to the last one sent within ALERT_REPEAT_HOURS, so the
    2-hourly task doesn't repeat the same "ACTIVE but not on eBay" message all day.
    A clean run (alert None) clears the state so a recurrence alerts again. Dry runs
    neither suppress nor write state. The alert is recorded when returned, and the
    Telegram send swallows failures — a failed send stays suppressed until the window
    ends (the Run Log notes still show it).
    """
    if dry_run:
        return alert
    now = time.time() if now is None else now
    try:
        if not alert:
            if os.path.exists(ALERT_STATE_PATH):
                os.remove(ALERT_STATE_PATH)
            return alert
        digest = hashlib.sha1(alert.encode("utf-8")).hexdigest()
        try:
            with open(ALERT_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        if (state.get("hash") == digest
                and now - float(state.get("sent_at", 0)) < ALERT_REPEAT_HOURS * 3600):
            logger.info("ebay_sync: identical alert already sent within "
                        f"{ALERT_REPEAT_HOURS}h — not re-sending")
            return None
        with open(ALERT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"hash": digest, "sent_at": now}, f)
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f"ebay_sync: alert de-dup state unavailable ({e}) — sending anyway")
    return alert


# ── Mirror eBay's live price into col H ───────────────────────────────────────

def mirror_live_prices(report: dict, service, COL, sheet_name: str, *, skip_ids=(),
                       title_reader=None, dry_run: bool = False) -> list[dict]:
    """
    Col H follows eBay: for every matched row (any status — every matched row is on eBay's
    ActiveList, and the active monitor's margin check reads col H) whose col H differs from eBay's live
    Buy It Now price (by >= $0.005, or is blank), write col H = the eBay price. Runs LAST in
    run_ebay_sync. skip_ids = items whose price changed this run (auto-repriced, col BD
    applied): they already have col H written, and the listings data is pre-revision, so
    mirroring them would write a stale price back. Never calls eBay, never touches col BE
    (manual_cost) — this syncs the price, not Jay's intent.

    Titles are re-read once before the first write; a row whose title changed (auditor
    shift) is skipped ("row_moved"). Returns [{row_num, title, item_id, old_price,
    new_price, outcome: mirrored|dry_run|row_moved|error}].
    """
    skip = set(skip_ids or ())
    todo = []
    for m in report.get("matched", []):
        price = m.get("price")
        if price is None or m.get("item_id") in skip:
            continue
        old = m.get("sheet_price")
        if old is not None and abs(old - price) < 0.005:
            continue
        todo.append({"row_num": m["row_num"], "title": m.get("title", ""),
                     "item_id": m.get("item_id"), "old_price": old, "new_price": price,
                     "outcome": None})
    if not todo:
        return []
    if dry_run:
        for t in todo:
            t["outcome"] = "dry_run"
        return todo

    titles = None
    if title_reader is not None:
        try:
            titles = title_reader()
        except Exception as e:
            logger.error(f"ebay_sync: col H mirror skipped — could not re-verify titles: {e}")
            for t in todo:
                t["outcome"] = "error"
            return todo
    first = True
    for t in todo:
        if titles is not None and str(titles.get(t["row_num"], "")).strip() != str(t["title"]).strip():
            t["outcome"] = "row_moved"
            logger.warning(f"ebay_sync: row {t['row_num']} changed since read — col H not mirrored")
            continue
        if not first:
            _sleep(WRITE_DELAY)
        first = False
        try:
            safe_write_row(service, sheet_name, t["row_num"], [(COL["ebay_price"], t["new_price"])])
            t["outcome"] = "mirrored"
            old = f"${t['old_price']:.2f}" if t["old_price"] is not None else "blank"
            logger.info(f"ebay_sync: row {t['row_num']} col H {old} -> ${t['new_price']:.2f} "
                        f"(eBay live price)")
        except Exception as e:
            t["outcome"] = "error"
            logger.error(f"ebay_sync: col H mirror failed for row {t['row_num']}: {e}")
    return todo


def format_mirror_summary(results: list[dict]) -> str | None:
    """Telegram HTML block for col H prices the mirror overwrote (or would, on a dry run), or
    None. A hand edit to col H is replaced by eBay's price, so it must never be silent."""
    done = [r for r in results or [] if r.get("outcome") in ("mirrored", "dry_run")]
    if not done:
        return None

    def _line(r):
        old = f"${r['old_price']:.2f}" if r.get("old_price") is not None else "blank"
        return (f"• {html.escape(_truncate(r['title']))} — {old} → ${r['new_price']:.2f} "
                f"(row {r['row_num']})")
    lines = _section(f"\n✏️ <b>Col H set to eBay's live price ({len(done)})</b>",
                     [_line(r) for r in done])
    lines.append("To change a live price from the sheet, use col BD (target price), not col H.")
    return "\n".join(lines)


# ── Auto-reprice hard breaches to break-even ──────────────────────────────────
#
# The ONE eBay write that runs without a tap (Jay's call, 2026-10-05). Guards: ACTIVE rows
# only (PAUSED_* = Jay is on it), never lower, a raise above AUTO_MAX_JUMP_PCT is held for a
# manual look (a raise that big usually means bad cost data in col G/BB, e.g. a variant
# mismatch, not a real loss), and col H is written only when the row still holds the same
# title (the auditor may have shifted rows).
#
# Manual price wins, then re-arms: a row whose col BE (manual_cost) is filled was priced by Jay
# against that Costco cost. While col G still equals BE the row is skipped (m["auto"] = "manual";
# the hard alert keeps showing the loss). Once G moves away from BE his decision is stale: the
# row is repriced as usual and BE is cleared with the col H write.

AUTO_MAX_JUMP_PCT = 0.50   # same threshold as tools/reprice.BIG_JUMP_PCT (circular import)


def auto_reprice_breaches(report: dict, service, COL, sheet_name: str, *, title_reader=None,
                          dry_run: bool = False, enabled: bool = True,
                          max_jump_pct: float = AUTO_MAX_JUMP_PCT) -> list[dict]:
    """
    Push every HARD breach (net < 0) on a LIVE listing to round_up_99(break_even) via
    revise_fixed_price, then write the new price to col H. Marks each handled breach
    m["auto"] = "repriced" | "held" | "failed" | "dry_run" (alert_message drops "repriced").

    Returns [{item_id, title, row_num, old_price, new_price, ok, outcome, error, sheet_note,
    rearmed}]. Any sheet status qualifies (PAUSED_MARGIN, AUDIT_REVIEW, WATCH, ...): every
    margin_breach entry is a row matched by col Q to a listing on eBay's ActiveList, so it is
    live and can sell at a loss. Skipped silently: soft breaches, missing item id / price /
    break-even, break-even already <= the live price. Skipped as "manual" (not in the result):
    col BE filled and col G == BE, or G unknown (can't tell the cost moved). rearmed = BE was
    filled but stale (G != BE) — BE is cleared together with col H. Never raises for eBay errors; an auth error stops
    the loop (every later call would fail the same way).
    """
    from tools.reprice import log_revise, round_up_99   # reprice imports this module

    if not enabled:
        return []
    results, titles = [], None
    for m in report.get("margin_breach", []):
        # No status gate: a live listing on a non-ACTIVE row (PAUSED_MARGIN, AUDIT_REVIEW, ...)
        # loses money just the same. margin_breach only holds rows matched to ActiveList.
        if m.get("severity") != "hard":
            continue
        item_id, be, old_price = m.get("item_id"), m.get("break_even"), m.get("ebay_price")
        title, row_num = m.get("title", ""), m.get("row_num")
        if not item_id or not be or not old_price:
            continue
        new_price = round_up_99(be)
        if new_price <= old_price + 0.005:
            continue

        manual_cost = _to_float(m.get("manual_cost", ""))
        rearmed = False
        if manual_cost is not None:
            cost_now = _to_float(m.get("costco_cost", ""))
            if cost_now is None or abs(cost_now - manual_cost) < 0.005:
                m["auto"] = "manual"
                logger.info(f"auto_reprice: item {item_id} row {row_num} kept at manual "
                            f"${old_price:.2f} (priced against cost ${manual_cost:.2f})")
                continue
            rearmed = True
            logger.info(f"auto_reprice: item {item_id} row {row_num} re-armed — Costco cost "
                        f"${manual_cost:.2f} -> ${cost_now:.2f} since the manual price")

        entry = {"item_id": item_id, "title": title, "row_num": row_num,
                 "old_price": old_price, "new_price": new_price,
                 "ok": False, "outcome": None, "error": None, "sheet_note": None,
                 "rearmed": rearmed}
        results.append(entry)

        if new_price > old_price * (1 + max_jump_pct):
            entry["outcome"] = m["auto"] = "held"
            entry["error"] = f"raise >{max_jump_pct:.0%} — check cost (col G/BB) before repricing"
            logger.warning(f"auto_reprice: HELD item {item_id} ${old_price:.2f}→${new_price:.2f} "
                           f"({title[:40]}) — jump >{max_jump_pct:.0%}")
            continue
        if dry_run:
            entry["ok"], entry["outcome"] = True, "dry_run"
            m["auto"] = "dry_run"
            logger.info(f"auto_reprice (dry run): item {item_id} "
                        f"${old_price:.2f}→${new_price:.2f} ({title[:40]})")
            continue

        result = revise_fixed_price(item_id, new_price)
        log_revise(service, result, title=title, row=row_num, old_price=old_price, source="auto")
        if not result.get("ok"):
            entry["outcome"] = m["auto"] = "failed"
            entry["error"] = (f"{result.get('error_code') or result.get('error_kind') or 'error'}: "
                              f"{result.get('message', '')}")
            if result.get("error_kind") == "auth":
                entry["error"] = "eBay token rejected — generate a new EBAY_AUTH_TOKEN"
                logger.critical("auto_reprice: auth error — stopping")
                break
            continue

        entry["ok"], entry["outcome"] = True, "repriced"
        m["auto"] = "repriced"
        # eBay is updated — sync col H, only if the row still holds this title.
        try:
            if titles is None:
                titles = title_reader() if title_reader else {}
            if title_reader and titles.get(row_num, "").strip() != str(title).strip():
                entry["sheet_note"] = "eBay OK — sheet col H NOT updated (row moved)"
                logger.warning(f"auto_reprice: row {row_num} title changed — col H not written")
            else:
                cols = [(COL["ebay_price"], new_price)]
                if rearmed and COL.get("manual_cost"):
                    cols.append((COL["manual_cost"], ""))     # back to the unprotected state
                safe_write_row(service, sheet_name, row_num, cols)
                _sleep(WRITE_DELAY)
        except Exception as e:
            entry["sheet_note"] = f"eBay OK — sheet col H write failed: {e}"[:120]
            logger.error(f"auto_reprice: item {item_id} revised but col H write failed: {e}")
    return results


def format_auto_reprice_summary(results: list[dict]) -> str | None:
    """Telegram HTML block for the eBay sync alert, or None when nothing was attempted."""
    if not results:
        return None
    lines = ["🔧 <b>Auto-repriced to break-even</b>"]
    for r in results:
        t = html.escape(_truncate(r.get("title", "")))
        op = f"${r['old_price']:.2f}" if r.get("old_price") else "?"
        np_ = f"${r['new_price']:.2f}"
        row = f" (row {r['row_num']})" if r.get("row_num") else ""
        outcome = r.get("outcome")
        if outcome == "dry_run":
            lines.append(f"• {t} — {op}→{np_} (dry run, not sent){row}")
        elif outcome == "repriced":
            note = f" — ⚠️ {html.escape(r['sheet_note'])}" if r.get("sheet_note") else ""
            why = " — Costco cost changed since your manual price" if r.get("rearmed") else ""
            lines.append(f"• ✓ {t} — {op}→{np_}{row}{why}{note}")
        elif outcome == "held":
            lines.append(f"• ⏸ {t} — NOT repriced ({op}→{np_}): "
                         f"{html.escape(r.get('error') or '')}{row}")
        else:
            err = html.escape((r.get("error") or "unknown error")[:80])
            lines.append(f"• ❌ {t} — reprice to {np_} failed: {err}{row}")
    return "\n".join(lines)


# ── Sheet-driven bulk reprice (col BD target_price) ───────────────────────────
#
# Jay types a price in col BD on ACTIVE rows; this pushes it to eBay. It's HIS number, so there
# is no "never lower" and no big-jump hold — below break-even and a >TARGET_WARN_JUMP_PCT change
# are warnings only. On success col H = target, BD cleared, BE (manual_cost) = col G — the key
# that makes auto_reprice_breaches leave this price alone until Costco's cost moves.

TARGET_WARN_JUMP_PCT = 0.50


def _target_entry(row, item_id, target, outcome=None, error=None):
    return {"row_num": row.get("row_num"), "title": row.get("title", ""), "item_id": item_id,
            "target": target, "old_price": None, "outcome": outcome, "error": error,
            "warnings": [], "sheet_note": None}


def _target_warnings(row, target, ref_price, warn_jump_pct) -> list[str]:
    """Typo guard + below-break-even note for a target (never blocks)."""
    out = []
    if ref_price and abs(target - ref_price) / ref_price > warn_jump_pct:
        out.append(f"{(target - ref_price) / ref_price:+.0%} change from ${ref_price:.2f} — typo?")
    cost = _cost_basis(row.get("buy_cost"), row.get("costco_cost"))
    fee = _to_rate(row.get("fee_rate", ""))
    if cost is not None and fee is not None:
        ship = _to_float(row.get("ship_cost", "")) or 0.0
        ad = _to_float(row.get("ad_cost", "")) or 0.0
        net = compute_net(target, cost, fee, ship, ad)
        if net < 0:
            be = _break_even(cost, fee, ship, ad)
            be_txt = f" (break-even ${be:.2f})" if be else " (below break-even)"
            out.append(f"loses ${abs(net):.2f}/sale{be_txt}")
    return out


def apply_target_prices(service, COL, sheet_name, start_row, end_row, *, rows=None,
                        dry_run=False, row_reader=None,
                        warn_jump_pct=TARGET_WARN_JUMP_PCT) -> dict:
    """
    Push every col BD target price on an ACTIVE row to eBay.

    Per row: BD blank -> ignored. Not ACTIVE / bad BD value / no item id in col Q / item id in
    several rows -> "skipped" (BD kept). GetItem live price: error -> "failed" (auth stops the
    sweep); listing not Active on eBay or multi-variation -> "skipped". target == live -> "noop"
    (H synced to live, BD cleared, BE = G). Else ReviseFixedPriceItem -> "applied" (H = target,
    BD cleared, BE = G) or "failed" (BD kept for the next run; auth stops the sweep). Every eBay
    write is logged via log_revise(source="manual").

    Before writing, the row is re-read (row_reader(row_num) -> {title, target_price} | None):
    a different title = the auditor shifted rows -> nothing written ("row moved"); a different
    BD = Jay typed a new price mid-run -> H/BE written but BD kept so the new one applies next.

    rows: load_sheet_rows-shaped dicts (loaded when None). They're updated in place on success
    (ebay_price, target_price, manual_cost) so a caller's later margin check sees the new state.
    dry_run: no eBay calls at all (not even GetItem) and no writes.
    Returns {results: [entry], stopped: bool, dry_run: bool}. Never raises for eBay errors.
    """
    from tools.reprice import log_revise   # reprice imports this module

    report = {"results": [], "stopped": False, "dry_run": dry_run}
    bd_col, be_col = COL.get("target_price"), COL.get("manual_cost")
    if not bd_col:
        return report
    if rows is None:
        rows = load_sheet_rows(service, COL, sheet_name, start_row, end_row)
    if row_reader is None:
        row_reader = lambda n: _read_row(service, COL, sheet_name, n)   # noqa: E731

    id_rows = {}
    for r in rows:
        iid = extract_item_id(r.get("ebay_listing_url"))
        if iid:
            id_rows.setdefault(iid, []).append(r["row_num"])

    first_write = True
    for row in rows:
        raw_target = str(row.get("target_price", "") or "").strip()
        if not raw_target:
            continue
        item_id = extract_item_id(row.get("ebay_listing_url"))
        target = _to_float(raw_target)
        entry = _target_entry(row, item_id, round(target, 2) if target is not None else None)
        report["results"].append(entry)

        status = str(row.get("status", "")).strip().upper()
        if status != "ACTIVE":
            entry.update(outcome="skipped", error=f"not ACTIVE ({status or 'blank'}) — BD only "
                                                  "applies to live listings")
            continue
        if target is None or target <= 0:
            entry.update(outcome="skipped", error=f"BD {raw_target!r} is not a valid price")
            continue
        target = entry["target"]
        if not item_id:
            entry.update(outcome="skipped", error="no eBay item id in col Q — fill col Q")
            continue
        if len(id_rows.get(item_id, [])) > 1:
            entry.update(outcome="skipped", error=f"item {item_id} is in col Q of "
                                                  f"{len(id_rows[item_id])} rows — fix the duplicate")
            continue

        if dry_run:
            ref = _to_float(row.get("ebay_price", ""))
            entry.update(outcome="dry_run", old_price=ref)
            entry["warnings"] = _target_warnings(row, target, ref, warn_jump_pct)
            logger.info(f"apply_targets (dry run): item {item_id} -> ${target:.2f} (row {row['row_num']})")
            continue

        live = fetch_item_price(item_id)
        if not live.get("ok"):
            entry.update(outcome="failed", error=(f"{live.get('error_code') or live.get('error_kind')}: "
                                                  f"couldn't read the live price — {live.get('message', '')}"))
            if live.get("error_kind") == "auth":
                entry["error"] = "eBay token rejected — generate a new EBAY_AUTH_TOKEN"
                report["stopped"] = True
                logger.critical("apply_targets: auth error on GetItem — stopping")
                break
            continue
        current = live["price"]
        entry["old_price"] = current
        if live.get("listing_status") and live["listing_status"] != "Active":
            entry.update(outcome="skipped", error=f"listing is {live['listing_status']} on eBay")
            continue
        if live.get("has_variations"):
            entry.update(outcome="skipped", error="multi-variation listing — set the price per "
                                                  "variation in Seller Hub")
            continue
        entry["warnings"] = _target_warnings(row, target, current, warn_jump_pct)

        if abs(current - target) < 0.005:
            entry["outcome"] = "noop"
            write_price = current
        else:
            result = revise_fixed_price(item_id, target)
            log_revise(service, result, title=entry["title"], row=row["row_num"],
                       old_price=current, source="manual")
            if not result.get("ok"):
                entry.update(outcome="failed",
                             error=(f"{result.get('error_code') or result.get('error_kind') or 'error'}: "
                                    f"{result.get('message', '')}"))
                if "variation" in str(result.get("message", "")).lower():
                    entry["error"] += " (multi-variation listing — set it in Seller Hub)"
                if result.get("error_kind") == "auth":
                    entry["error"] = "eBay token rejected — generate a new EBAY_AUTH_TOKEN"
                    report["stopped"] = True
                    logger.critical("apply_targets: auth error — stopping")
                    break
                continue
            entry["outcome"] = "applied"
            write_price = target

        # eBay matches the target — sync the sheet if the row is still this one.
        cost_now = _to_float(row.get("costco_cost", ""))
        snapshot = cost_now if cost_now is not None else ""
        try:
            fresh = row_reader(row["row_num"])
            if not fresh or str(fresh.get("title", "")).strip() != str(entry["title"]).strip():
                entry["sheet_note"] = "eBay OK — sheet NOT updated (row moved); clear BD by hand"
                logger.warning(f"apply_targets: row {row['row_num']} title changed — nothing written")
                continue
            cols = [(COL["ebay_price"], write_price)]
            bd_unchanged = _to_float(fresh.get("target_price", "")) == _to_float(raw_target)
            if bd_unchanged:
                cols.append((bd_col, ""))
            else:
                entry["sheet_note"] = "BD changed during the run — new price applies next run"
            if be_col:
                cols.append((be_col, snapshot))
            if not first_write:
                _sleep(WRITE_DELAY)
            first_write = False
            safe_write_row(service, sheet_name, row["row_num"], cols)
            row["ebay_price"] = write_price
            if bd_unchanged:
                row["target_price"] = ""
            row["manual_cost"] = snapshot
        except Exception as e:
            entry["sheet_note"] = f"eBay OK — sheet write failed: {e}"[:120]
            logger.error(f"apply_targets: item {item_id} at ${write_price:.2f} but sheet write failed: {e}")
    return report


def summarize_targets(report: dict) -> str:
    """'targets applied 2 noop 1 failed 0 skipped 1' for the Run Log notes ('' when nothing)."""
    results = report.get("results") or []
    if not results:
        return ""
    counts = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    order = ("applied", "noop", "dry_run", "failed", "skipped")
    s = "targets " + " ".join(f"{k} {counts[k]}" for k in order if counts.get(k))
    n_warn = sum(1 for r in results if r.get("warnings"))
    if n_warn:
        s += f" (warnings {n_warn})"
    return s


def format_target_summary(report: dict) -> str | None:
    """Telegram HTML block for the sheet-driven price changes, or None when BD was empty."""
    results = report.get("results") or []
    if not results:
        return None
    dry = " (dry run)" if report.get("dry_run") else ""
    lines = [f"💲 <b>Price changes from the sheet (col BD){dry}</b>"]
    for r in results[:MAX_REPORT_LINES]:
        t = html.escape(_truncate(r.get("title", "")))
        row = f" (row {r['row_num']})" if r.get("row_num") else ""
        tgt = f"${r['target']:.2f}" if r.get("target") is not None else "?"
        op = f"${r['old_price']:.2f}" if r.get("old_price") is not None else "?"
        outcome = r.get("outcome")
        if outcome == "applied":
            line = f"• ✓ {t} — {op}→{tgt}{row}"
        elif outcome == "noop":
            line = f"• ✓ {t} — already {tgt} on eBay, BD cleared{row}"
        elif outcome == "dry_run":
            line = f"• {t} — would set {op}→{tgt} (dry run, not sent){row}"
        elif outcome == "failed":
            line = (f"• ❌ {t} — {tgt} NOT applied: {html.escape((r.get('error') or '')[:100])}"
                    f"{row} — BD kept, retried next run")
        else:
            line = f"• ⏭ {t} — skipped: {html.escape(r.get('error') or '')}{row}"
        for w in r.get("warnings") or []:
            line += f"\n   ⚠️ {html.escape(w)}"
        if r.get("sheet_note"):
            line += f"\n   ⚠️ {html.escape(r['sheet_note'])}"
        lines.append(line)
    if len(results) > MAX_REPORT_LINES:
        lines.append(f"…and {len(results) - MAX_REPORT_LINES} more")
    if report.get("stopped"):
        lines.append("🔑 Stopped early — eBay rejected the token; the rest stay in BD.")
    return "\n".join(lines)


# ── True sale reconciliation (GetItemTransactions) ────────────────────────────

TXN_WINDOW_DAYS = 30   # GetItemTransactions rejects a ModTimeFrom..ModTimeTo span over 30 days
TXN_MAX_DAYS    = 90   # ...and keeps no transactions older than ~90 days


def _build_get_item_transactions_xml(token: str, item_id: str, days_back: int = 30, *,
                                     start=None, end=None) -> str:
    """Window = [start, end] when given, else the last days_back days (callers keep it <= 30)."""
    end = end or datetime.now(timezone.utc)
    start = start or (end - timedelta(days=days_back))
    fr = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    selectors = "".join(f"<OutputSelector>{s}</OutputSelector>" for s in (
        "TransactionArray.Transaction.TransactionPrice",
        "TransactionArray.Transaction.FinalValueFee",
        "TransactionArray.Transaction.QuantityPurchased",
        "TransactionArray.Transaction.CreatedDate",
        "TransactionArray.Transaction.TransactionID",
        "TransactionArray.Transaction.MonetaryDetails",   # refunds (cancellations / returns)
        "TransactionArray.Transaction.ShippedTime",
    ))
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<GetItemTransactionsRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>"
        "<DetailLevel>ReturnAll</DetailLevel>"
        f"<ItemID>{_xml_escape(str(item_id))}</ItemID>"
        f"<ModTimeFrom>{fr}</ModTimeFrom><ModTimeTo>{to}</ModTimeTo>"
        f"{selectors}"
        "</GetItemTransactionsRequest>"
    )


def fetch_item_transactions(item_id, days_back: int = 30) -> list[dict]:
    """Actual sales for one listing via GetItemTransactions. Each entry holds the REAL
    price and the REAL FinalValueFee eBay charged — the inputs for reconciling true net.
    days_back over TXN_WINDOW_DAYS is fetched as consecutive <=30-day windows (eBay rejects a
    longer span), capped at TXN_MAX_DAYS, merged and de-duplicated by transaction_id.
    Returns [{transaction_id, price, fee, quantity, created}]; [] on any failure in ANY window
    (missing creds / network / Ack=Failure / bad XML) — a partial history would read as "no
    sales" in the missing weeks. Never raises."""
    iid = extract_item_id(item_id)
    if not iid:
        return []
    token, app_id, dev_id, cert_id, err_kind, err_msg = _load_credentials()
    if err_kind:
        logger.warning(f"fetch_item_transactions: skipped — {err_msg}")
        return []
    days = max(1, min(int(days_back or 30), TXN_MAX_DAYS))
    end = datetime.now(timezone.utc)
    oldest = end - timedelta(days=days)
    out, seen = [], set()
    while end > oldest:
        start = max(oldest, end - timedelta(days=TXN_WINDOW_DAYS))
        chunk = _fetch_transactions_window(token, app_id, dev_id, cert_id, iid, start, end)
        if chunk is None:
            return []
        for t in chunk:
            key = t["transaction_id"] or f"{t['created']}|{t['price']}"
            if key not in seen:
                seen.add(key)
                out.append(t)
        end = start
    return out


def _fetch_transactions_window(token, app_id, dev_id, cert_id, iid, start, end) -> list[dict] | None:
    """One GetItemTransactions call for [start, end]; None on any failure."""
    try:
        raw = _post(_build_get_item_transactions_xml(token, iid, start=start, end=end),
                    _headers(app_id, dev_id, cert_id, "GetItemTransactions"))
    except OSError as e:
        logger.error(f"fetch_item_transactions: eBay request failed: {e}")
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.error(f"fetch_item_transactions: unparseable response: {e}")
        return None
    if _text(root, "Ack") not in ("Success", "Warning"):
        errors = _parse_errors(root)
        logger.error(f"fetch_item_transactions: eBay error — "
                     + ("; ".join(f"{c}: {m}" for c, m in errors) or "Ack not Success"))
        return None
    out = []
    for txn in root.findall("{*}TransactionArray/{*}Transaction"):
        refunded = 0.0
        for r in txn.findall("{*}MonetaryDetails/{*}Refunds/{*}Refund"):
            if _text(r, "RefundStatus") == "Succeeded":
                refunded += abs(_num(_text(r, "RefundAmount"), float, 0.0) or 0.0)
        out.append({
            "transaction_id": _text(txn, "TransactionID"),
            "price":          _num(_text(txn, "TransactionPrice"), float, None),
            "fee":            _num(_text(txn, "FinalValueFee"), float, None),
            "quantity":       _num(_text(txn, "QuantityPurchased"), int, 1),
            "created":        _text(txn, "CreatedDate"),
            "refunded":       round(refunded, 2),
            "shipped":        bool(_text(txn, "ShippedTime")),
        })
    return out


def estimate_fee(price, quantity, fee_rate) -> float | None:
    """eBay fee for one transaction the way col I models it (price x qty x fee_rate x 1.08 buyer
    tax + $0.30/order). Used when GetItemTransactions sends no FinalValueFee — with managed
    payments it never does (verified live 2026-10-16; the real fee is only in the Sell Finances
    API, which needs an OAuth user token). None when price or fee rate is unknown."""
    if price is None or fee_rate is None:
        return None
    qty = max(int(quantity or 1), 1)
    return round(price * qty * fee_rate * EBAY_FEE_TAX_FACTOR + EBAY_FEE_FLAT, 2)


def actual_net_per_unit(txn: dict, cost_basis) -> float | None:
    """Actual net per unit for one transaction: (price - fee/qty) - cost_basis*(1+tax).
    None when price/fee/cost are unknown."""
    if txn.get("price") is None or txn.get("fee") is None or cost_basis is None:
        return None
    qty = max(txn.get("quantity") or 1, 1)
    payout = txn["price"] - txn["fee"] / qty
    return round(payout - cost_basis * (1 + COSTCO_TAX_RATE), 2)


def sync_actual_sales(service, sheet_name, col_map, rows, listings, *, dry_run=False) -> dict:
    """Reconcile TRUE sale net: for each matched listing that has sold, pull its
    transactions, append every new one to the sales log (data/sales_log.json — timestamped,
    feeds the bot's Made / 15-90-day net), and write the most recent sale's actual net/unit
    into col 'actual_net'. An item not yet in the log (or with a logged sale whose net is still
    unknown) is fetched with TXN_MAX_DAYS (90) of history; otherwise the last 30 days. A sale on a row with no cost is still logged
    (net None). Dry run writes neither the log nor the sheet.
    Returns {written, skipped, logged, failed: [(item_id, msg)]}. Never raises."""
    from tools import sales_log

    stats = {"written": 0, "skipped": 0, "logged": 0, "failed": []}
    actual_col = col_map.get("actual_net") if isinstance(col_map, dict) else None
    log = sales_log.load()
    sold_ids = {l["item_id"] for l in listings if (l.get("quantity_sold") or 0) > 0}
    # plus items already in the log: a sold-out listing drops off ActiveList, but its logged
    # sales still need refund / cancellation updates
    sold_ids |= {iid for iid in log if log.get(iid)}
    for row in rows:
        iid = extract_item_id(row.get("ebay_listing_url"))
        if not iid or iid not in sold_ids:
            continue
        cost = _cost_basis(row.get("buy_cost"), row.get("costco_cost"))
        fee_rate = _to_rate(row.get("fee_rate", ""))
        days = TXN_MAX_DAYS if sales_log.needs_backfill(log, iid) else 30
        try:
            txns = fetch_item_transactions(iid, days_back=days)
        except Exception as e:
            stats["failed"].append((iid, f"fetch: {e}"))
            continue
        if not txns:
            stats["skipped"] += 1
            continue
        txns = [t if t.get("fee") is not None else
                {**t, "fee": estimate_fee(t.get("price"), t.get("quantity"), fee_rate),
                 "fee_estimated": True}
                for t in txns]
        stats["logged"] += sales_log.add_transactions(log, iid, txns, cost)
        kept = [t for t in txns if not sales_log._is_cancelled(
            t.get("price"), t.get("quantity"), t.get("refunded"), t.get("shipped"))]
        if not kept:
            stats["skipped"] += 1
            continue
        latest = max(kept, key=lambda t: t.get("created") or "")
        net = actual_net_per_unit(latest, cost)
        if net is None or not actual_col:
            stats["skipped"] += 1
            continue
        if not dry_run:
            safe_write_row(service, sheet_name, row["row_num"], [(actual_col, net)])
        stats["written"] += 1
    if (stats["logged"] or log != sales_log.load()) and not dry_run:
        try:
            sales_log.save(log)
        except OSError as e:
            stats["failed"].append(("sales_log", f"save: {e}"))
            logger.error(f"ebay_sync: sales log save failed: {e}")
    return stats


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

    # The margin read reaches col BB (buy_cost); make sure the grid does. Best-effort.
    try:
        ensure_grid_columns(service, sheet_name, required_grid_columns(COL))
    except Exception as e:
        logger.warning(f"ebay_sync: grid-size check failed (continuing): {e}")

    rows = load_sheet_rows(service, COL, sheet_name, start_row, end_row)
    auto_cfg = ((config or {}).get("business") or {}).get("ebay_sync") or {}

    # Sheet-driven price changes (col BD) go FIRST: the margin check below then sees the new
    # live price and the fresh col BE snapshot, so auto-reprice can't undo Jay's number.
    target_report, target_summary = None, None
    if auto_cfg.get("apply_target_prices", True):
        try:
            target_report = apply_target_prices(
                service, COL, sheet_name, start_row, end_row, rows=rows, dry_run=dry_run,
                warn_jump_pct=float(auto_cfg.get("target_warn_jump_pct", TARGET_WARN_JUMP_PCT)))
            now_live = {r["item_id"]: r["target"] for r in target_report["results"]
                        if r["outcome"] in ("applied", "noop")}
            for listing in listings:
                if listing["item_id"] in now_live:
                    listing["price"] = now_live[listing["item_id"]]
            target_summary = format_target_summary(target_report)
        except Exception as e:  # never let the sweep break the units_sold sync
            logger.exception(f"ebay_sync: target-price sweep failed: {e}")

    # True-sale reconciliation first: it refreshes the sales log (new sales, refunds) that
    # sync() reads to take cancelled units out of col U, and writes col 'actual_net'.
    try:
        actual = sync_actual_sales(service, sheet_name, COL, rows, listings, dry_run=dry_run)
        if actual["written"]:
            logger.info(f"ebay_sync: actual net written for {actual['written']} sold listing(s)")
        if actual["logged"]:
            logger.info(f"ebay_sync: {actual['logged']} new sale(s) added to the sales log")
    except Exception as e:  # never break the sync over reconciliation
        logger.exception(f"ebay_sync: actual-sale reconciliation failed: {e}")

    report = sync(
        service, rows, listings, dry_run=dry_run, sheet_name=sheet_name, col_map=COL,
        title_reader=lambda: _read_titles(service, COL, sheet_name, start_row, end_row),
        floor_price=float(auto_cfg.get("floor_price", FLOOR_PRICE)),
        floor_margin_pct=float(auto_cfg.get("floor_margin_pct", FLOOR_MARGIN_PCT)),
    )
    # Auto-reprice hard breaches BEFORE building the alert (repriced ones leave "Losing money").
    auto_summary = None
    auto_results = []
    try:
        auto_results = auto_reprice_breaches(
            report, service, COL, sheet_name,
            title_reader=lambda: _read_titles(service, COL, sheet_name, start_row, end_row),
            dry_run=dry_run, enabled=bool(auto_cfg.get("auto_reprice", True)),
            max_jump_pct=float(auto_cfg.get("auto_reprice_max_jump_pct", AUTO_MAX_JUMP_PCT)))
        auto_summary = format_auto_reprice_summary(auto_results)
    except Exception as e:  # never let auto-reprice break the units_sold sync
        logger.exception(f"ebay_sync: auto-reprice failed: {e}")

    # Last: col H mirrors eBay's live price — except items whose price changed this run.
    changed = {r["item_id"] for r in auto_results or [] if r.get("outcome") == "repriced"}
    if target_report:
        changed |= {r["item_id"] for r in target_report["results"]
                    if r["outcome"] in ("applied", "noop")}
    try:
        report["h_mirror"] = mirror_live_prices(
            report, service, COL, sheet_name, skip_ids=changed, dry_run=dry_run,
            title_reader=lambda: _read_titles(service, COL, sheet_name, start_row, end_row))
    except Exception as e:  # never let the mirror break the sync
        logger.exception(f"ebay_sync: col H mirror failed: {e}")

    base_alert = alert_message(report)
    mirror_summary = format_mirror_summary(report.get("h_mirror"))
    extras = [b for b in (target_summary, auto_summary, mirror_summary) if b]
    if base_alert and extras:
        combined_alert = "\n".join([base_alert] + extras)
    elif extras:
        combined_alert = "\n".join(["🛒 <b>eBay sync</b>" + (" (dry run)" if dry_run else "")]
                                   + extras)
    else:
        combined_alert = base_alert

    digest = digest_message(report)
    if digest and not _digest_due(dry_run=dry_run):
        digest = None
    notes = summarize(report)
    if target_report and summarize_targets(target_report):
        notes += ", " + summarize_targets(target_report)
    result = {"status": "ok", "notes": notes,
              "alert": _dedupe_alert(combined_alert, dry_run=dry_run),
              "digest": digest}
    if report["write_errors"]:
        result["status"] = "error"
        result["errors"] = "; ".join(
            f"row {w['row_num']}: {w['error']}" for w in report["write_errors"])[:300]
    # 30-day view counts (Sell Analytics, user OAuth token) -> data/listing_views.json, at most
    # once per ~24h; a failure keeps the stale cache and never fails the sync.
    if not dry_run:
        try:
            from tools.ebay_analytics import refresh_views_cache
            result["notes"] += ", " + refresh_views_cache()
        except Exception as e:
            logger.warning(f"ebay_sync: views refresh skipped: {e}")
    logger.info(f"ebay_sync: {result['notes']}")
    return result
