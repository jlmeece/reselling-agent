"""
Tool: ebay_taxonomy
Authoritative eBay item specifics (aspects) per category, from the Taxonomy API.

Auth is OAuth client-credentials (application token) — EBAY_APP_ID / EBAY_CERT_ID.
It does NOT use EBAY_AUTH_TOKEN (that is the Trading API user token).

  get_item_aspects(category_id) -> list[dict] | None
      list  = the API answered; these are the REQUIRED aspects ([] = none required)
      None  = unavailable (no creds, API/network error, invalid or non-leaf category)
              — callers fall back to the hand-kept ebay_required_specifics

Each aspect: {"name", "mode" (FREE_TEXT|SELECTION_ONLY), "values", "multi", "max_length"}.
`values` is only stored for SELECTION_ONLY aspects (capped) so Brand-style lists don't
bloat the cache.

Nothing here raises. Results are cached in data/ebay_taxonomy_cache.json (30 days;
a permanently bad category ID is cached negatively for 1 day).

USAGE:
  python tools/ebay_taxonomy.py --check     # validate every category ID in categories.yaml
                                            # and report drift vs ebay_required_specifics
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from loguru import logger

load_dotenv(encoding="utf-8", override=True)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
ASPECTS_URL = ("https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/"
               "get_item_aspects_for_category")   # tree 0 = eBay US
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
REQUEST_TIMEOUT = 20

CACHE_PATH = Path(__file__).parent.parent / "data" / "ebay_taxonomy_cache.json"
CACHE_TTL_SECONDS = 30 * 86400        # aspects change rarely
NEGATIVE_TTL_SECONDS = 86400          # bad / non-leaf category ID
FAILURE_COOLDOWN_SECONDS = 60         # after a transient failure, don't hammer a down API
TOKEN_SKEW_SECONDS = 60
MAX_STORED_VALUES = 500

_now = time.time      # indirections so tests can control time and skip sleeps
_sleep = time.sleep

_token = {"value": None, "expires_at": 0.0}
_token_fail_until = 0.0
_fail_until: dict = {}        # category_id -> ts; transient-failure memo (in-memory only)
_cache: dict | None = None    # in-memory copy of CACHE_PATH, loaded once per process
_warned: set = set()


def reset_state() -> None:
    """Clear all in-memory state (token, cache copy, cooldowns). Used by tests."""
    global _token_fail_until, _cache
    _token["value"], _token["expires_at"] = None, 0.0
    _token_fail_until = 0.0
    _fail_until.clear()
    _warned.clear()
    _cache = None


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg)


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _http_json(req: urllib.request.Request) -> tuple[int, dict]:
    """Send `req`, return (status, parsed JSON body). HTTP errors return their status;
    network errors propagate (callers go through _send). The single seam tests patch."""
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        return e.code, body


def _send(req: urllib.request.Request) -> tuple[int, dict]:
    """_http_json that never raises: network/parse failures come back as (0, {})."""
    try:
        status, body = _http_json(req)
        return status, body if isinstance(body, dict) else {}
    except Exception as e:
        logger.debug(f"  eBay taxonomy request failed: {e}")
        return 0, {}


# ── OAuth ────────────────────────────────────────────────────────────────────

def _get_oauth_token(force_refresh: bool = False) -> str | None:
    """Application access token (client-credentials), cached until ~1 min before expiry.
    Returns None (and logs) on any failure; a failure suppresses retries for a minute."""
    global _token_fail_until
    now = _now()
    if not force_refresh and _token["value"] and now < _token["expires_at"]:
        return _token["value"]
    if now < _token_fail_until:
        return None

    app_id = os.getenv("EBAY_APP_ID", "").strip()
    cert_id = os.getenv("EBAY_CERT_ID", "").strip()
    if not (app_id and cert_id):
        _warn_once("no_creds", "eBay Taxonomy skipped — EBAY_APP_ID / EBAY_CERT_ID not set")
        return None

    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": OAUTH_SCOPE}).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    status, data = _send(req)
    token = data.get("access_token") if status == 200 else None
    if not token:
        _token["value"], _token["expires_at"] = None, 0.0
        _token_fail_until = now + FAILURE_COOLDOWN_SECONDS
        logger.warning(f"eBay OAuth token request failed (HTTP {status}): "
                       f"{data.get('error_description') or data.get('error') or 'no detail'}")
        return None

    try:
        ttl = int(data.get("expires_in", 7200))
    except (TypeError, ValueError):
        ttl = 7200
    _token["value"] = token
    _token["expires_at"] = now + max(ttl - TOKEN_SKEW_SECONDS, 0)
    return token


# ── Cache ────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = {}
        try:
            if CACHE_PATH.exists():
                loaded = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    _cache = loaded
        except Exception as e:
            logger.warning(f"eBay taxonomy cache unreadable, starting fresh: {e}")
    return _cache


def _save_cache() -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(_load_cache(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        logger.warning(f"eBay taxonomy cache write failed: {e}")


def _cache_get(category_id: str):
    """(hit, aspects). A fresh negative entry is a hit with aspects=None."""
    entry = _load_cache().get(category_id)
    if not isinstance(entry, dict):
        return False, None
    aspects = entry.get("aspects")
    ttl = CACHE_TTL_SECONDS if isinstance(aspects, list) else NEGATIVE_TTL_SECONDS
    if _now() - float(entry.get("fetched_at", 0)) < ttl:
        return True, aspects if isinstance(aspects, list) else None
    return False, None


def _cache_put(category_id: str, aspects) -> None:
    _load_cache()[category_id] = {"fetched_at": _now(), "aspects": aspects}
    _save_cache()


# ── Aspects ──────────────────────────────────────────────────────────────────

def _is_required(aspect: dict) -> bool:
    # The documented field is aspectConstraint.aspectRequired (bool); also accept the
    # `requirement: REQUIRED` spelling in case the API/docs use it.
    c = aspect.get("aspectConstraint") or {}
    return c.get("aspectRequired") is True or str(c.get("requirement", "")).upper() == "REQUIRED"


def _normalize(aspect: dict) -> dict | None:
    name = str(aspect.get("localizedAspectName") or aspect.get("name") or "").strip()
    if not name:
        return None
    c = aspect.get("aspectConstraint") or {}
    mode = str(c.get("aspectMode") or "FREE_TEXT").upper()
    values = []
    if mode == "SELECTION_ONLY":
        for v in aspect.get("aspectValues") or []:
            text = str(v.get("localizedValue") or "").strip() if isinstance(v, dict) else ""
            if text:
                values.append(text)
        values = values[:MAX_STORED_VALUES]
    try:
        max_length = int(c["aspectMaxLength"]) if c.get("aspectMaxLength") else None
    except (TypeError, ValueError):
        max_length = None
    return {
        "name": name,
        "mode": mode,
        "values": values,
        "multi": str(c.get("itemToAspectCardinality", "")).upper() == "MULTI",
        "max_length": max_length,
    }


def _fetch_aspects(category_id: str):
    """One category's raw response with 401 re-auth and a single 429/5xx retry.
    Returns (status, body); status 0 = no token / network failure."""
    reauthed = retried = False
    while True:
        token = _get_oauth_token()
        if not token:
            return 0, {}
        req = urllib.request.Request(
            f"{ASPECTS_URL}?{urllib.parse.urlencode({'category_id': category_id})}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        status, body = _send(req)
        if status == 401 and not reauthed:
            reauthed = True
            _token["value"], _token["expires_at"] = None, 0.0
            continue
        if status in (0, 429, 500, 502, 503, 504) and not retried:
            retried = True
            _sleep(1.5)
            continue
        return status, body


def get_item_aspects(category_id, refresh: bool = False):
    """REQUIRED aspects for an eBay US category. See module docstring for the contract
    (list = authoritative answer, None = unavailable). Never raises."""
    try:
        cid = str(category_id or "").strip()
        if not cid:
            return None
        if not refresh:
            hit, aspects = _cache_get(cid)
            if hit:
                return aspects
            if _now() < _fail_until.get(cid, 0.0):
                return None

        status, body = _fetch_aspects(cid)
        raw = body.get("aspects")
        if status == 200 and isinstance(raw, list):
            aspects = [a for a in (_normalize(x) for x in raw if isinstance(x, dict) and _is_required(x)) if a]
            _cache_put(cid, aspects)
            return aspects
        if status in (400, 404):
            msg = (body.get("errors") or [{}])[0].get("message", "") if isinstance(body.get("errors"), list) else ""
            logger.warning(f"eBay category {cid} has no item aspects (HTTP {status}) — "
                           f"invalid or non-leaf category? {msg}")
            _cache_put(cid, None)
            return None
        logger.warning(f"eBay Taxonomy aspects for category {cid} unavailable (HTTP {status})")
        _fail_until[cid] = _now() + FAILURE_COOLDOWN_SECONDS
        return None
    except Exception as e:   # never raise into the export
        logger.warning(f"eBay Taxonomy lookup crashed for category {category_id}: {e}")
        return None


# ── CLI: validate categories.yaml against the API ────────────────────────────

def check_categories() -> int:
    """Print validity + drift for every category ID in config/categories.yaml.
    Returns the number of IDs the API did not confirm."""
    import yaml
    with open(Path(__file__).parent.parent / "config" / "categories.yaml", encoding="utf-8") as f:
        categories = yaml.safe_load(f)["categories"]

    unconfirmed = 0
    for name, cfg in categories.items():
        ids = {}
        for cid in [cfg.get("ebay_category_id"), *(cfg.get("ebay_category_map") or {}).values()]:
            if cid:
                ids[str(cid)] = None
        yaml_req = {k.casefold(): k for k in cfg.get("ebay_required_specifics", [])}
        for cid in ids:
            aspects = get_item_aspects(cid, refresh=True)
            if aspects is None:
                unconfirmed += 1
                print(f"[{name}] {cid}: NOT CONFIRMED (invalid/non-leaf category, or API unavailable)")
                continue
            api = {("C:" + a["name"]).casefold(): a["name"] for a in aspects}
            print(f"[{name}] {cid}: ok - API requires {sorted(api.values()) or 'nothing'}")
            api_only = [v for k, v in api.items() if k not in yaml_req]
            yaml_only = [v for k, v in yaml_req.items() if k not in api]
            if api_only:
                print(f"    API requires, yaml lacks: {api_only}")
            if yaml_only:
                print(f"    yaml lists, API doesn't require: {yaml_only}")
    return unconfirmed


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
    if "--check" in sys.argv:
        sys.exit(1 if check_categories() else 0)
    print(__doc__)
