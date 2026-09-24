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

  get_category_suggestions(title) -> list[dict] | None
      Category suggestions for a product title, in eBay's relevance order (best first):
      [{"id", "name", "level", "path"}, ...]; [] = eBay had no match, None = unavailable.
      Deliberately NOT re-sorted by tree depth: every suggestion is already a leaf, and
      deepest-first promoted junk (air fryer -> commercial deep fryers, watch -> watch dials).

Nothing here raises. Results are cached in data/ebay_taxonomy_cache.json (30 days;
a permanently bad category ID and an empty suggestion result are cached for 1 day).

USAGE:
  python tools/ebay_taxonomy.py --check     # for every category in categories.yaml: is each
                                            # hardcoded ID a valid leaf, what does eBay suggest
                                            # for a representative title, and how do the required
                                            # aspects drift from ebay_required_specifics
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
TREE_URL = "https://api.ebay.com/commerce/taxonomy/v1/category_tree/0"   # tree 0 = eBay US
ASPECTS_URL = f"{TREE_URL}/get_item_aspects_for_category"
SUGGESTIONS_URL = f"{TREE_URL}/get_category_suggestions"
MAX_QUERY_CHARS = 200
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


def _cache_ttl(field: str, value) -> int:
    """Long for a real answer; short for a negative one (bad category ID -> None, or a
    title eBay had no suggestion for -> [])."""
    if isinstance(value, list) and not (field == "suggestions" and not value):
        return CACHE_TTL_SECONDS
    return NEGATIVE_TTL_SECONDS


def _cache_get(key: str, field: str = "aspects"):
    """(hit, value). Category-ID keys hold "aspects"; "q:<title>" keys hold "suggestions".
    A fresh negative entry is a hit (aspects None, or suggestions [])."""
    entry = _load_cache().get(key)
    if not isinstance(entry, dict) or field not in entry:
        return False, None
    value = entry[field]
    if _now() - float(entry.get("fetched_at", 0)) < _cache_ttl(field, value):
        return True, value if isinstance(value, list) else None
    return False, None


def _cache_put(key: str, value, field: str = "aspects") -> None:
    _load_cache()[key] = {"fetched_at": _now(), field: value}
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


def _authed_get(url: str):
    """GET with the app token, 401 re-auth (once) and a single 429/5xx retry.
    Returns (status, body); status 0 = no token / network failure."""
    reauthed = retried = False
    while True:
        token = _get_oauth_token()
        if not token:
            return 0, {}
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
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

        status, body = _authed_get(f"{ASPECTS_URL}?{urllib.parse.urlencode({'category_id': cid})}")
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


# ── Category suggestions ─────────────────────────────────────────────────────

def _normalize_suggestion(entry: dict) -> dict | None:
    cat = entry.get("category") or {}
    cid = str(cat.get("categoryId") or "").strip()
    if not cid:
        return None
    try:
        level = int(entry.get("categoryTreeNodeLevel") or 0)
    except (TypeError, ValueError):
        level = 0

    def _lvl(a):
        try:
            return int(a.get("categoryTreeNodeLevel") or 0)
        except (TypeError, ValueError):
            return 0

    ancestors = [a for a in (entry.get("categoryTreeNodeAncestors") or []) if isinstance(a, dict)]
    path = [str(a.get("categoryName") or "") for a in sorted(ancestors, key=_lvl)]   # root first
    path.append(str(cat.get("categoryName") or ""))
    return {"id": cid, "name": str(cat.get("categoryName") or ""), "level": level,
            "path": " > ".join(p for p in path if p)}


def get_category_suggestions(title, refresh: bool = False):
    """Category suggestions for a product title in eBay's relevance order (best first).
    `level` is informational only — see the module docstring for why we don't sort on it.
    list = eBay's answer ([] = no match), None = unavailable. Never raises."""
    try:
        query = " ".join(str(title or "").split())[:MAX_QUERY_CHARS].rstrip()
        if not query:
            return None
        key = "q:" + query.casefold()
        if not refresh:
            hit, cached = _cache_get(key, "suggestions")
            if hit:
                return cached
            if _now() < _fail_until.get(key, 0.0):
                return None

        status, body = _authed_get(f"{SUGGESTIONS_URL}?{urllib.parse.urlencode({'q': query})}")
        if status == 200:
            raw = body.get("categorySuggestions") or []
            found = [x for x in (_normalize_suggestion(e) for e in raw if isinstance(e, dict)) if x]
            _cache_put(key, found, "suggestions")
            return found
        logger.warning(f"eBay category suggestions for '{query[:40]}' unavailable (HTTP {status})")
        _fail_until[key] = _now() + FAILURE_COOLDOWN_SECONDS
        return None
    except Exception as e:
        logger.warning(f"eBay category suggestion lookup crashed for '{str(title)[:40]}': {e}")
        return None


# ── CLI: validate categories.yaml against the API ────────────────────────────

KNOWLEDGE_DIR = Path(__file__).parent.parent / "data" / "knowledge" / "products"


def _load_known_titles(knowledge_dir=None) -> dict:
    """{category: [titles]} from the local knowledge store (no network)."""
    titles: dict = {}
    for f in sorted(Path(knowledge_dir or KNOWLEDGE_DIR).glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("title") and d.get("category"):
            titles.setdefault(d["category"], []).append(str(d["title"]))
    return titles


def _representative_title(category: str, keyword: str, known: dict) -> str:
    """A real product title for this category (containing `keyword` when there is one);
    falls back to the keyword itself, then the category name."""
    pool = known.get(category, [])
    if keyword and keyword != "default":
        for t in pool:
            if keyword.casefold() in t.casefold():
                return t
        return keyword
    return pool[0] if pool else category


def _drift_status(hardcoded_id: str, hardcoded_valid: bool, suggestions) -> str:
    """OK = same ID; STALE = hardcoded ID isn't a valid leaf; DIFFERS = both valid but
    different; NO SUGGESTION = eBay returned nothing usable."""
    if not suggestions:
        return "STALE (no suggestion)" if not hardcoded_valid else "NO SUGGESTION"
    if not hardcoded_valid:
        return "STALE"
    return "OK" if suggestions[0]["id"] == str(hardcoded_id) else "DIFFERS"


def check_category_suggestions(categories: dict, known: dict) -> int:
    """Print hardcoded-vs-suggested for every yaml category entry. Returns the number of
    entries that are not OK."""
    not_ok = 0
    for name, cfg in categories.items():
        entries = []                                    # (keyword, hardcoded id)
        if cfg.get("ebay_category_id"):
            entries.append(("default", str(cfg["ebay_category_id"])))
        for kw, cid in (cfg.get("ebay_category_map") or {}).items():
            if kw != "default" and cid:
                entries.append((kw, str(cid)))
        for kw, cid in entries:
            title = _representative_title(name, kw, known)
            synthetic = title not in known.get(name, [])      # no real product matched the keyword
            valid = get_item_aspects(cid, refresh=True) is not None
            sugg = get_category_suggestions(title, refresh=True)
            status = _drift_status(cid, valid, sugg)
            if status != "OK":
                not_ok += 1
            top = sugg[0] if sugg else None
            print(f"[{name}] '{kw}'  yaml {cid} ({'valid leaf' if valid else 'NOT a valid leaf'})  ->  {status}")
            print(f"    title: {title[:70]}" + ("   (synthetic - no real product matched; weak signal)" if synthetic else ""))
            if top:
                print(f"    suggested: {top['id']} {top['name']} (level {top['level']})  {top['path']}")
                extra = [f"{x['id']} {x['name']}" for x in sugg[1:3]]
                if extra:
                    print(f"    also:      {'; '.join(extra)}")
            elif sugg is None:
                print("    suggested: (API unavailable)")
    return not_ok


def check_categories() -> int:
    """Print validity + drift for every category ID in config/categories.yaml.
    Returns the number of IDs the API did not confirm."""
    import yaml
    with open(Path(__file__).parent.parent / "config" / "categories.yaml", encoding="utf-8") as f:
        categories = yaml.safe_load(f)["categories"]

    print("=== Category resolution: yaml ID vs eBay suggestion ===")
    check_category_suggestions(categories, _load_known_titles())
    print("\n=== Required aspects: API vs ebay_required_specifics ===")

    unconfirmed = 0
    for name, cfg in categories.items():
        ids = {}
        for cid in [cfg.get("ebay_category_id"), *(cfg.get("ebay_category_map") or {}).values()]:
            if cid:
                ids[str(cid)] = None
        yaml_req = {k.casefold(): k for k in cfg.get("ebay_required_specifics", [])}
        for cid in ids:
            aspects = get_item_aspects(cid)      # cache-warm from the pass above
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
