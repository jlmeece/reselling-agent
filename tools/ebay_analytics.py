"""
Tool: ebay_analytics
eBay OAuth USER token + Sell Analytics traffic report (per-listing view counts).

The Trading API sends no view count for this account (GetItem HitCount / ActiveList are empty),
so views come from Sell Analytics, which needs a user token (not EBAY_AUTH_TOKEN, not the
client-credentials app token the Taxonomy API uses):

    EBAY_OAUTH_REFRESH_TOKEN  ~18-month refresh token from eBay's consent flow (the bare
                              "v^1.…" string, or the whole token-response JSON pasted as-is)
    EBAY_APP_ID / EBAY_CERT_ID  the same client pair as ebay_taxonomy (Basic auth)

get_user_access_token() trades the refresh token for a 2-hour access token, cached in
data/.ebay_user_token.json until ~1 min before expiry. get_listing_views() pulls ONE traffic
report covering every listing (eBay aggregates views daily, so the caller caches it ~24h —
refresh_views_cache() keeps data/listing_views.json {fetched_at, days, views: {item_id: n}}).

Live-verified 2026-10-21: the report dimension is LISTING (ITEM is error 50001 "The dimension
is invalid"); filter = marketplace_ids:{EBAY_US},date_range:[YYYYMMDD..YYYYMMDD]; host
api.ebay.com. Every public function returns None on failure and never raises.

Spec: docs/superpowers/specs/2026-10-21-oauth-user-token-views.md
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TRAFFIC_URL = "https://api.ebay.com/sell/analytics/v1/traffic_report"
SCOPES = (
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",   # views (this module)
    "https://api.ebay.com/oauth/api_scope/sell.finances",             # fees (follow-up spec)
)
TOKEN_PATH = _ROOT / "data" / ".ebay_user_token.json"
VIEWS_PATH = _ROOT / "data" / "listing_views.json"
TOKEN_SKEW_SECONDS = 60
VIEWS_MAX_AGE_HOURS = 24
REQUEST_TIMEOUT = 30


def _now() -> float:
    return time.time()


def _http_json(req: urllib.request.Request) -> tuple[int, dict]:
    """Send `req` -> (status, parsed JSON). HTTP errors return their status + body; network
    errors propagate (callers go through _send). The single seam tests patch."""
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        return e.code, body


def _send(req) -> tuple[int, dict]:
    try:
        status, body = _http_json(req)
        return status, body if isinstance(body, dict) else {}
    except Exception as e:
        logger.warning(f"ebay_analytics: request failed: {e}")
        return 0, {}


def _error_text(status, data) -> str:
    errs = data.get("errors") if isinstance(data.get("errors"), list) else []
    if errs:
        e = errs[0] if isinstance(errs[0], dict) else {}
        return f"HTTP {status} {e.get('errorId', '')}: {e.get('message') or e.get('longMessage') or ''}".strip()
    return f"HTTP {status}: {data.get('error_description') or data.get('error') or 'no detail'}"


def _refresh_token() -> str:
    """EBAY_OAUTH_REFRESH_TOKEN — the bare token, or the refresh_token field when the whole
    token-response JSON was pasted into .env."""
    raw = os.getenv("EBAY_OAUTH_REFRESH_TOKEN", "").strip()
    if raw.startswith("{"):
        try:
            return str(json.loads(raw).get("refresh_token") or "").strip()
        except (ValueError, AttributeError):
            return ""
    return raw


def _read_json(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


last_token_error = ""     # why the last get_user_access_token() returned None ("" = it didn't)


def get_user_access_token(force=False, scopes=None, cache_path=None) -> str | None:
    """User access token from the refresh token, cached until ~1 min before expiry.
    scopes / cache_path default to SCOPES / TOKEN_PATH; another scope set (the Media API's
    sell.inventory) needs its own cache file — a token only carries the scopes it was minted
    with, and asking for a scope the refresh token was never consented for fails the WHOLE
    request (invalid_scope), so it can't be folded into SCOPES.
    force=True skips the cache. None on any failure (logged, reason in last_token_error),
    never raises."""
    global last_token_error
    scopes = SCOPES if scopes is None else scopes
    cache_path = TOKEN_PATH if cache_path is None else cache_path
    now = _now()
    if not force:
        cached = _read_json(cache_path)
        if cached.get("access_token") and now < float(cached.get("expires_at") or 0):
            last_token_error = ""
            return cached["access_token"]
    refresh = _refresh_token()
    app_id = os.getenv("EBAY_APP_ID", "").strip()
    cert_id = os.getenv("EBAY_CERT_ID", "").strip()
    if not (refresh and app_id and cert_id):
        last_token_error = "EBAY_OAUTH_REFRESH_TOKEN / EBAY_APP_ID / EBAY_CERT_ID not set"
        logger.warning(f"ebay_analytics: {last_token_error}")
        return None
    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh,
                                   "scope": " ".join(scopes)}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Authorization": f"Basic {basic}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    status, data = _send(req)
    token = data.get("access_token") if status == 200 else None
    if not token:
        last_token_error = _error_text(status, data)
        if data.get("error") == "invalid_scope":
            last_token_error += (f" — the refresh token was never consented for "
                                 f"{', '.join(s.rsplit('/', 1)[-1] for s in scopes)}")
        logger.warning(f"ebay_analytics: user token refresh failed — {last_token_error}")
        return None
    last_token_error = ""
    try:
        ttl = int(data.get("expires_in", 7200))
    except (TypeError, ValueError):
        ttl = 7200
    try:
        _write_json(cache_path, {"access_token": token,
                                 "expires_at": now + max(ttl - TOKEN_SKEW_SECONDS, 0)})
    except OSError as e:
        logger.warning(f"ebay_analytics: couldn't cache the user token ({e})")
    return token


def get_listing_views(days=30) -> dict[str, int] | None:
    """{item_id: views} over the last `days` full days (yesterday back), for every listing in
    one traffic-report call. None on any failure (token, auth/scope, empty, parse)."""
    token = get_user_access_token()
    if not token:
        return None
    end = date.today() - timedelta(days=1)          # today's views aren't aggregated yet
    start = end - timedelta(days=max(int(days), 1) - 1)
    query = urllib.parse.urlencode({
        "dimension": "LISTING", "metric": "LISTING_VIEWS_TOTAL",
        "filter": f"marketplace_ids:{{EBAY_US}},date_range:[{start:%Y%m%d}..{end:%Y%m%d}]",
    })
    req = urllib.request.Request(f"{TRAFFIC_URL}?{query}",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/json"})
    status, data = _send(req)
    if status != 200:
        logger.warning(f"ebay_analytics: traffic report failed — {_error_text(status, data)}")
        return None
    views = {}
    for rec in data.get("records") or []:
        try:
            item_id = str(rec["dimensionValues"][0]["value"]).strip()
            n = int(float(rec["metricValues"][0]["value"]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if item_id:
            views[item_id] = n
    if not views:
        logger.warning("ebay_analytics: traffic report had no usable records")
        return None
    return views


def load_views_cache() -> dict:
    """data/listing_views.json -> {fetched_at, days, views}; {} when missing/unreadable."""
    data = _read_json(VIEWS_PATH)
    return data if isinstance(data.get("views"), dict) else {}


def cached_views(item_id) -> int | None:
    """Cached 30-day views for one listing, or None (no cache / no entry)."""
    try:
        n = load_views_cache().get("views", {}).get(str(item_id))
        return int(n) if n is not None else None
    except (TypeError, ValueError):
        return None


def refresh_views_cache(days=30, max_age_hours=VIEWS_MAX_AGE_HOURS, force=False) -> str:
    """Re-fetch the traffic report when the cache is missing or older than max_age_hours.
    -> short status for Run Log notes. A failed fetch keeps the stale cache. Never raises."""
    try:
        cache = load_views_cache()
        fetched = cache.get("fetched_at")
        if not force and fetched:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched)
                if age < timedelta(hours=max_age_hours):
                    return f"views cached ({len(cache.get('views', {}))})"
            except (TypeError, ValueError):
                pass                                   # unreadable timestamp = refetch
        views = get_listing_views(days)
        if views is None:
            logger.warning("ebay_analytics: views fetch failed — keeping the stale cache")
            return "views fetch failed (kept cache)" if cache else "views fetch failed"
        _write_json(VIEWS_PATH, {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "days": days, "views": views})
        return f"views refreshed ({len(views)})"
    except Exception as e:   # contract: never raises
        logger.warning(f"ebay_analytics: views refresh error: {e}")
        return "views fetch failed"


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env", encoding="utf-8", override=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    v = get_listing_views()
    if v is None:
        print("❌ no view counts (see the warning above)")
        sys.exit(1)
    top = sorted(v.items(), key=lambda kv: -kv[1])[:10]
    print(f"✓ {len(v)} listings, {sum(v.values())} views (30d). Top: "
          + ", ".join(f"{k}={n}" for k, n in top))
