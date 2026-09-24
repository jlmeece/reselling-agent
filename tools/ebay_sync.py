"""
eBay sync — Stage 1 (read-only on eBay, flag-only on the sheet)
===============================================================
Pulls Jay's active eBay listings via the Trading API (GetMyeBaySelling), matches
them to Product Tracker rows by the item ID in col Q (ebay_listing_url), and:

  * writes QuantitySold into units_sold (col U) — the ONLY write, and only when it
    changed (through tools.sheet_writer.safe_write_row);
  * REPORTS (never fixes) margin breaches (live eBay price vs cost — NOT raw price
    movement; Jay undercuts by a cent himself), eBay listings missing from the sheet,
    and ACTIVE sheet rows that eBay no longer lists.

Margin check: net = live price - cost_basis - price*fee_rate - ship - ad, where
cost_basis = buy_cost (col BB, manual) if present else costco_cost (col G).
HARD (Telegram per run): net < 0. SOFT (once-a-day digest): 0 <= net < $4 AND
sold_90d == 0. Everything else is silent.

Never touches eBay data and never changes any price. Run via
`python agents/scheduler.py --mode ebay_sync [--dry-run]`.

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
    """buy_cost (what Jay actually paid) if present, parseable and > 0, else costco_cost."""
    bc = _to_float(buy_cost)
    if bc is not None and bc > 0:
        return bc
    return _to_float(costco_cost)


def compute_net(ebay_price, cost_basis, fee_rate, ship=0.0, ad=0.0) -> float:
    """Mirrors the sheet's net_profit (I = H - G - AC - AD - AE) with the live eBay price
    for H and cost_basis for G."""
    return ebay_price - cost_basis - ebay_price * fee_rate - (ship or 0.0) - (ad or 0.0)


def margin_flag(ebay_price, cost_basis, fee_rate, ship, ad, sold_90d):
    """
    "hard" (net < 0) | "soft" (0 <= net < MARGIN_SOFT_FLOOR and sold_90d == 0) | None.
    Unknown price / cost / fee rate -> None (can't judge; never assume $0). Missing ship/ad
    count as 0. sold_90d None (blank) is unknown, NOT 0, so a thin-margin row with no
    velocity data stays silent; high-velocity items ride at $3.99 silently.
    """
    if ebay_price is None or cost_basis is None or fee_rate is None:
        return None
    net = round(compute_net(ebay_price, cost_basis, fee_rate, ship, ad), 2)
    if net < 0:
        return "hard"
    if net < MARGIN_SOFT_FLOOR and sold_90d == 0:
        return "soft"
    return None


def _break_even(cost_basis, fee_rate, ship, ad):
    """Lowest price at which net >= 0 (ad treated as a fixed amount)."""
    if fee_rate >= 1:
        return None
    return (cost_basis + (ship or 0.0) + (ad or 0.0)) / (1 - fee_rate)


MARGIN_COLS = ("buy_cost", "costco_cost", "fee_rate", "ship_cost", "ad_cost", "sold_90d")


def load_sheet_rows(service, COL, sheet_name, start_row, end_row) -> list[dict]:
    """Read the tracker into dicts (row_num = absolute sheet row). Skips empty rows."""
    raw = read_sheet(service, f"'{sheet_name}'!A{start_row}:BB{end_row}")
    idx = {k: _col_idx(COL[k]) for k in
           ("status", "title", "platform", "ebay_price", "ebay_listing_url", "units_sold")}
    # margin inputs — tolerant of a col_map that predates them
    idx.update({k: _col_idx(COL[k]) for k in MARGIN_COLS if k in COL})
    rows = []
    for offset, r in enumerate(raw):
        row = {k: _cell(r, i) for k, i in idx.items()}
        # emptiness judged on the identity columns only — margin inputs can be formula noise
        if not any(str(row[k]).strip() for k in
                   ("status", "title", "platform", "ebay_price", "ebay_listing_url", "units_sold")):
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

def _norm_title(s) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed — for title comparison."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s).lower()).split())


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
        toks = set(norm.split())
        best_score, best = None, []
        for l, ln in pool_norm:
            if ln == norm:
                score, kind = (1, 1.0), "exact"
            else:
                union = toks | set(ln.split())
                jac = len(toks & set(ln.split())) / len(union) if union else 0.0
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


def _check_margin(row, listing, report):
    """Evaluate one matched row's margin at the LIVE eBay price; append to
    report["margin_breach"] (hard/soft) or report["margin_unchecked"] (inputs unknown)."""
    price = listing["price"]
    cost = _cost_basis(row.get("buy_cost"), row.get("costco_cost"))
    fee = _to_rate(row.get("fee_rate", ""))
    ship = _to_float(row.get("ship_cost", "")) or 0.0
    ad = _to_float(row.get("ad_cost", "")) or 0.0
    sold_raw = _to_float(row.get("sold_90d", ""))
    sold = None if sold_raw is None else int(sold_raw)
    base = {"row_num": row["row_num"], "title": row.get("title", ""),
            "item_id": listing["item_id"]}

    severity = margin_flag(price, cost, fee, ship, ad, sold)
    if price is None or cost is None or fee is None:
        report["margin_unchecked"].append(base)
    elif severity:
        report["margin_breach"].append({
            **base, "severity": severity, "ebay_price": price, "cost_basis": cost,
            "net": round(compute_net(price, cost, fee, ship, ad), 2),
            "break_even": _break_even(cost, fee, ship, ad)})


def sync(service, sheet_rows, listings, *, dry_run=False, sheet_name=None,
         col_map=None, title_reader=None) -> dict:
    """
    Match listings to sheet rows by the item ID in col Q. Flag-only: units_sold
    (col U) is the only thing ever written, and only when QuantitySold differs.

    sheet_rows:    dicts from load_sheet_rows (row_num, title, status, platform,
                   ebay_price, ebay_listing_url, units_sold, plus the margin inputs
                   buy_cost, costco_cost, fee_rate, ship_cost, ad_cost, sold_90d —
                   any may be absent/blank)
    title_reader:  optional callable -> {row_num: title}; called once before the
                   first write. A row whose title no longer matches (auditor deleted
                   rows since the read) is skipped, not written.

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
              "dry_run": dry_run}

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
            new_sold = listing["quantity_sold"]
            entry = {
                "row_num": row["row_num"], "title": title, "item_id": item_id,
                "old_units_sold": old_sold, "quantity_sold": new_sold,
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

            _check_margin(row, listing, report)
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
    hard = sum(1 for m in report["margin_breach"] if m["severity"] == "hard")
    soft = sum(1 for m in report["margin_breach"] if m["severity"] == "soft")
    s = (f"matched {len(report['matched'])}, updated {len(report['updated'])}, "
         f"margin_breach hard {hard} soft {soft}, "
         f"active_not_on_ebay {len(report['active_not_on_ebay'])}, "
         f"not_in_sheet {len(report['on_ebay_not_in_sheet'])}")
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
    Telegram HTML message, or None when there is nothing to flag. Only HARD margin
    breaches (net < 0) and ACTIVE-but-not-on-eBay rows warrant a message; SOFT breaches
    go to digest_message(), everything else stays in the Run Log. Raw price movement is
    never alerted. Sheet-derived titles are truncated THEN html-escaped.
    """
    losing = [m for m in report["margin_breach"] if m["severity"] == "hard"]
    gone = report["active_not_on_ebay"]
    if not losing and not gone:
        return None
    msg = ["🛒 <b>eBay sync</b>" + (" (dry run)" if report.get("dry_run") else "")]
    if losing:
        def _line(m):
            be = f", break-even ${m['break_even']:.2f}" if m.get("break_even") else ""
            return (f"• losing money on {html.escape(_truncate(m['title']))} — net "
                    f"-${abs(m['net']):.2f} (eBay ${m['ebay_price']:.2f}{be}) (row {m['row_num']})")
        msg += _section(f"\n💸 <b>Losing money ({len(losing)})</b>", [_line(m) for m in losing])
    if gone:
        msg += _section(f"\n⚠️ <b>ACTIVE but not on eBay ({len(gone)})</b>", [
            f"• {html.escape(_truncate(g['title']))} — {html.escape(g['reason'])} (row {g['row_num']})"
            for g in gone])
    extra = len(report["on_ebay_not_in_sheet"])
    if extra:
        msg.append(f"\nℹ️ {extra} eBay listing(s) not in the sheet")
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
    report = sync(
        service, rows, listings, dry_run=dry_run, sheet_name=sheet_name, col_map=COL,
        title_reader=lambda: _read_titles(service, COL, sheet_name, start_row, end_row),
    )
    digest = digest_message(report)
    if digest and not _digest_due(dry_run=dry_run):
        digest = None
    result = {"status": "ok", "notes": summarize(report),
              "alert": _dedupe_alert(alert_message(report), dry_run=dry_run),
              "digest": digest}
    if report["write_errors"]:
        result["status"] = "error"
        result["errors"] = "; ".join(
            f"row {w['row_num']}: {w['error']}" for w in report["write_errors"])[:300]
    logger.info(f"ebay_sync: {result['notes']}")
    return result
