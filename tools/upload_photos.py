"""
upload_photos.py — Host the branded listing photos (tools/photo_compositor.py) on eBay.

For every data/listing_photos/<sku>.jpg, uploads the JPEG to eBay Picture Services with the
Media API's createImageFromFile (multipart form-data, field "image") and records the hosted
URL in data/hosted_photos.json:

    {"<sku>": {"url": "https://i.ebayimg.com/...", "sha256": "...", "uploaded_at": "ISO",
               "expires_at": "ISO" (eBay's expirationDate), "gallery": [...]}}

The Trading API's UploadSiteHostedPictures is RETIRED: since late Oct 2026 it answers HTTP 404
"Resource not found" (eBay scheduled it for 2026-09-30; last success here 2026-10-21). The
Media API needs an OAuth USER token with the sell.inventory scope — minted from
EBAY_OAUTH_REFRESH_TOKEN (tools/ebay_analytics) and cached in data/.ebay_media_token.json.
A refresh token consented without sell.inventory fails with invalid_scope (live 2026-10-30):
redo the consent with that scope ticked (RECONSENT_HINT).

tools/ebay_export.py reads that map and puts the hosted URL FIRST in PicURL (the main
image), followed by the EPS-hosted gallery shots.

A photo is skipped when its sha256 matches the stored one (unchanged since upload) and it
is not near expiry: EXPIRY_MARGIN_DAYS before eBay's expirationDate, or, for older entries
with no expiry, REUPLOAD_AFTER_DAYS after the upload — eBay purges hosted pictures that never
get attached to a listing. A stale entry whose re-upload FAILS is dropped from the map, so
ebay_export never points a listing at a purged picture (it falls back to the Costco photos).

Usage:
  python tools/upload_photos.py              # upload new / changed photos
  python tools/upload_photos.py --dry-run    # show what would be uploaded, no API calls
  python tools/upload_photos.py --force      # re-upload everything regardless of hash
  python tools/upload_photos.py --sku X1     # just this SKU

Exit code: 0 = nothing failed, 1 = at least one upload failed (or no credentials).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import socket
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from tools import ebay_analytics  # noqa: E402 — OAuth user token (refresh-token grant)
from tools.ebay_sync import REQUEST_TIMEOUT, _sleep  # noqa: E402

ROOT        = Path(__file__).resolve().parent.parent
PHOTOS_DIR  = ROOT / "data" / "listing_photos"
HOSTED_PATH = ROOT / "data" / "hosted_photos.json"
MEDIA_URL   = "https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_file"
MEDIA_SCOPES = ("https://api.ebay.com/oauth/api_scope/sell.inventory",)
MEDIA_TOKEN_PATH = ROOT / "data" / ".ebay_media_token.json"
RECONSENT_HINT = ("Renew EBAY_OAUTH_REFRESH_TOKEN: redo eBay's OAuth consent (developer.ebay.com > "
                  "User Tokens > Get a Token from eBay via Your Application) with sell.inventory "
                  "ticked next to sell.analytics.readonly and sell.finances, then put the new "
                  "refresh token in .env (quoted).")
REUPLOAD_AFTER_DAYS = 25     # entries with no expires_at: refresh after this (unlisted pictures get purged)
EXPIRY_MARGIN_DAYS = 5       # entries with expires_at: refresh this long before eBay's expirationDate
MAX_PHOTO_BYTES = 12 * 1024 * 1024   # eBay's per-picture upload limit
MAX_GALLERY = 23             # eBay allows 24 pictures/listing: 1 main + up to 23 gallery shots

_GALLERY_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8",
}


# ── Map file ──────────────────────────────────────────────────────────────────

def load_hosted(path: Path | str = None) -> dict:
    """sku -> {url, sha256, uploaded_at}. Missing/corrupt file = {} (never raises)."""
    path = Path(path or HOSTED_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning(f"upload_photos: unreadable {path.name} ({e}) — starting empty")
        return {}
    return data if isinstance(data, dict) else {}


def save_hosted(data: dict, path: Path | str = None) -> None:
    """Atomic write: temp file in the same dir + os.replace."""
    path = Path(path or HOSTED_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _parse_ts(value) -> datetime | None:
    """ISO timestamp (eBay's '…Z' included) -> aware datetime, None when missing/bad."""
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def hosted_url_for(sku: str, hosted: dict, now: datetime = None) -> str | None:
    """The hosted URL for a SKU, or None — also None once eBay's expirationDate has passed
    (the picture is purged; a listing pointing at it would post without it). Used by ebay_export."""
    entry = hosted.get(sku) if sku else None
    url = entry.get("url") if isinstance(entry, dict) else None
    if not (isinstance(url, str) and url.startswith("https://")):
        return None
    expires = _parse_ts(entry.get("expires_at"))
    if expires is not None and (now or datetime.now(timezone.utc)) >= expires:
        return None
    return url


# ── Upload (Media API createImageFromFile) ────────────────────────────────────

def _media_token() -> tuple:
    """(token, None, "") or (None, "auth", why + RECONSENT_HINT). Never raises."""
    try:
        token = ebay_analytics.get_user_access_token(scopes=MEDIA_SCOPES, cache_path=MEDIA_TOKEN_PATH)
    except Exception as e:  # contract: never raises
        token, ebay_analytics.last_token_error = None, f"unexpected error: {e}"
    if token:
        return token, None, ""
    why = ebay_analytics.last_token_error or "no OAuth user token"
    return None, "auth", f"eBay photo hosting has no Media API token ({why}). {RECONSENT_HINT}"


def _build_multipart(jpeg: bytes, filename: str, boundary: str) -> bytes:
    """multipart/form-data body: one part named "image" carrying the raw JPEG."""
    crlf, b = b"\r\n", boundary.encode()
    name = re.sub(r'[^A-Za-z0-9._-]', "_", filename).encode()
    return b"".join([
        b"--" + b + crlf,
        b'Content-Disposition: form-data; name="image"; filename="' + name + b'"' + crlf,
        b"Content-Type: image/jpeg" + crlf + crlf,
        jpeg + crlf,
        b"--" + b + b"--" + crlf,
    ])


def _json(raw: bytes) -> dict:
    try:
        data = json.loads((raw or b"").decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return {"_raw": (raw or b"")[:200].decode("utf-8", "replace")}
    return data if isinstance(data, dict) else {}


def _request(req) -> tuple[int, dict, dict]:
    """Send `req` -> (status, JSON body, headers); one retry on timeout / connection error /
    HTTP 5xx. HTTP 4xx returns its status + body. Raises OSError if both attempts fail."""
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT * 2) as resp:
                return resp.status, _json(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                return e.code, _json(e.read()), dict(e.headers or {})
            logger.warning(f"upload_photos: HTTP {e.code} from eBay — retrying once")
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            if attempt == 2:
                raise
            logger.warning(f"upload_photos: eBay request failed ({e}) — retrying once")
        _sleep(2)


def _post(jpeg: bytes, filename: str, token: str) -> tuple[int, dict, dict]:
    """POST one picture to createImageFromFile."""
    boundary = f"MIME_boundary_{uuid.uuid4().hex}"
    req = urllib.request.Request(
        MEDIA_URL, data=_build_multipart(jpeg, filename, boundary), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    return _request(req)


def _get_image(location: str, token: str) -> tuple[int, dict, dict]:
    """getImage — the 201 normally carries imageUrl; this is the fallback via Location."""
    req = urllib.request.Request(location, method="GET",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/json"})
    return _request(req)


def upload_picture(sku: str, jpeg: bytes, token: str) -> dict:
    """POST one picture. Returns {ok, url, expires_at, error_kind, message}. Never raises.
    error_kind: None | "auth" (HTTP 401/403 — token/scope) | "api" | "network"."""
    out = {"ok": False, "url": None, "expires_at": None, "error_kind": "api", "message": ""}
    try:
        status, body, headers = _post(jpeg, f"{sku}.jpg", token)
        if status in (200, 201) and not body.get("imageUrl"):
            loc = next((v for k, v in headers.items() if k.lower() == "location"), "")
            if loc:
                status, body, headers = _get_image(loc, token)
    except OSError as e:
        return dict(out, error_kind="network", message=f"eBay request failed: {e}")
    except Exception as e:  # contract: never raises
        return dict(out, message=f"unexpected error: {e}")
    url = body.get("imageUrl") or ""
    if status in (200, 201) and url.startswith("https://"):
        return dict(out, ok=True, url=url, expires_at=body.get("expirationDate"), error_kind=None)
    msg = ebay_analytics._error_text(status, body)
    if status in (200, 201):
        msg = f"HTTP {status} but no imageUrl in the response"
    elif body.get("_raw"):
        msg = f"HTTP {status}: {body['_raw']}"
    if status in (401, 403):
        return dict(out, error_kind="auth", message=f"{msg} — {RECONSENT_HINT}")
    return dict(out, message=msg)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_fresh(entry: dict, now: datetime) -> bool:
    """Not yet due for a re-upload: more than EXPIRY_MARGIN_DAYS before eBay's expirationDate
    when the entry has one, else uploaded within REUPLOAD_AFTER_DAYS. A missing/bad upload
    timestamp counts as fresh (older map entries are not re-uploaded en masse)."""
    expires = _parse_ts(entry.get("expires_at"))
    if expires is not None:
        return now < expires - timedelta(days=EXPIRY_MARGIN_DAYS)
    ts = _parse_ts(entry.get("uploaded_at"))
    if ts is None:
        return True
    return now - ts < timedelta(days=REUPLOAD_AFTER_DAYS)


def plan_uploads(photos: list[Path], hosted: dict, *, force=False, now=None) -> list[tuple]:
    """[(sku, path, bytes, sha, reason)] for photos needing an upload; reason = new/changed/stale/forced."""
    now = now or datetime.now(timezone.utc)
    todo = []
    for p in photos:
        data = p.read_bytes()
        sha = _sha256(data)
        entry = hosted.get(p.stem)
        if force:
            reason = "forced"
        elif not isinstance(entry, dict) or not entry.get("url"):
            reason = "new"
        elif entry.get("sha256") != sha:
            reason = "changed"
        elif not _is_fresh(entry, now):
            reason = "stale"
        else:
            continue
        todo.append((p.stem, p, data, sha, reason))
    return todo


def _drop_dead(hosted: dict, failed_skus, now: datetime) -> list[str]:
    """Remove the entries of failed uploads that are past their re-upload point: their URL is
    (about to be) purged, and leaving it would make ebay_export list a dead main picture.
    A fresh entry whose re-upload failed (changed / forced) still serves, so it stays."""
    dropped = []
    for s in failed_skus:
        entry = hosted.get(s)
        if isinstance(entry, dict) and entry.get("url") and not _is_fresh(entry, now):
            hosted.pop(s)
            dropped.append(s)
    return dropped


def run(*, dry_run=False, force=False, sku=None, photos_dir=None, hosted_path=None) -> dict:
    """Returns {uploaded, skipped, failed: [(sku, msg)], planned, dropped: [sku], auth_error}.
    auth_error = the token/scope message when uploading stopped on it ("" otherwise)."""
    photos_dir = Path(photos_dir or PHOTOS_DIR)
    hosted_path = Path(hosted_path or HOSTED_PATH)
    stats = {"uploaded": 0, "skipped": 0, "failed": [], "planned": 0, "dropped": [], "auth_error": ""}

    photos = sorted(photos_dir.glob("*.jpg")) if photos_dir.is_dir() else []
    if sku:
        photos = [p for p in photos if p.stem == sku]
        if not photos:
            logger.warning(f"No photo for SKU {sku} in {photos_dir}")
    hosted = load_hosted(hosted_path)
    if not hosted_path.exists() and not dry_run:
        save_hosted(hosted, hosted_path)      # create the map on first run

    todo = plan_uploads(photos, hosted, force=force)
    stats["skipped"] = len(photos) - len(todo)
    stats["planned"] = len(todo)
    if not todo:
        logger.info(f"upload_photos: nothing to upload ({len(photos)} photo(s), all current)")
        return stats

    if dry_run:
        for s, p, data, _sha, reason in todo:
            print(f"  would upload {s:<14} ({reason}, {len(data) // 1024} KB)  {p.name}")
        return stats

    now = datetime.now(timezone.utc)
    token, err_kind, err_msg = _media_token()
    if err_kind:
        logger.critical(f"upload_photos: {err_msg} — nothing uploaded")
        stats["failed"] = [(s, err_msg) for s, *_ in todo]
        stats["auth_error"] = err_msg
    else:
        for i, (s, p, data, sha, reason) in enumerate(todo):
            if len(data) > MAX_PHOTO_BYTES:
                res = {"ok": False, "error_kind": "invalid",
                       "message": f"{len(data) // 1024} KB exceeds eBay's 12 MB picture limit"}
            else:
                res = upload_picture(s, data, token)
            if res["ok"]:
                hosted[s] = {"url": res["url"], "sha256": sha,
                             "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                if res.get("expires_at"):
                    hosted[s]["expires_at"] = res["expires_at"]
                save_hosted(hosted, hosted_path)  # after every success — a crash keeps earlier URLs
                stats["uploaded"] += 1
                logger.info(f"✓ {s} -> {res['url']}")
            else:
                stats["failed"].append((s, res["message"]))
                logger.error(f"✗ {s}: {res['message']}")
                if res["error_kind"] == "auth":
                    # Same token for every call — the rest would fail identically.
                    rest = [t[0] for t in todo[i + 1:]]
                    stats["failed"] += [(r, "skipped — eBay token rejected") for r in rest]
                    stats["auth_error"] = res["message"]
                    logger.critical(f"upload_photos: eBay token rejected — stopping. {RECONSENT_HINT}")
                    break

    stats["dropped"] = _drop_dead(hosted, [s for s, _ in stats["failed"]], now)
    if stats["dropped"]:
        save_hosted(hosted, hosted_path)
        logger.warning(f"upload_photos: dropped {len(stats['dropped'])} expired hosted URL(s) whose "
                       f"re-upload failed: {', '.join(stats['dropped'])}")
    return stats


def _safe_sku(sku: str) -> str:
    """Sanitize a SKU to the hosted_photos.json key (matches photo_compositor.safe_sku
    and ebay_export._photo_key)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", sku.strip())


def _download_as_jpeg(url: str, timeout: int = 30) -> bytes:
    """Download an image URL and return JPEG bytes. Costco's CDN serves .avif-named files
    as image/jpeg, but re-encoding through PIL guarantees a valid JPEG regardless of source
    format, so eBay's EPS upload accepts it."""
    import requests
    from PIL import Image
    from io import BytesIO
    resp = requests.get(url, headers=_GALLERY_HEADERS, timeout=timeout)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content)).convert("RGB")
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


def upload_gallery(jobs, *, dry_run=False, force=False) -> dict:
    """Host the Costco gallery shots (col AT) on eBay EPS for every SKU that already has a
    hosted branded photo. eBay error 20004 forbids mixing EPS and self-hosted pictures in a
    single listing, so once the main photo is EPS the gallery must be EPS too — otherwise
    ebay_export falls back to the branded photo alone. Each SKU's gallery is uploaded once
    and cached under hosted_photos.json[sku]["gallery"].

    jobs: [{sku, urls, ...}] from photo_compositor.build_jobs (urls = col AT, sanitized).
    Returns {uploaded, skipped, failed: [(sku, msg)], auth_error}. Never raises."""
    stats = {"uploaded": 0, "skipped": 0, "failed": [], "auth_error": ""}
    if not jobs:
        return stats
    hosted = load_hosted()
    targets = []
    for job in jobs:
        sku = (job.get("sku") or "").strip()
        urls = [u for u in (job.get("urls") or [])
                if isinstance(u, str) and u.startswith(("http://", "https://"))]
        if not sku or not urls:
            continue
        key = _safe_sku(sku)
        entry = hosted.get(key)
        if not isinstance(entry, dict) or not entry.get("url"):
            continue                                   # no branded photo → no EPS gallery
        gallery_age = {"expires_at": entry.get("gallery_expires_at"),
                       "uploaded_at": entry.get("uploaded_at")}
        if entry.get("gallery") and not force and _is_fresh(gallery_age, datetime.now(timezone.utc)):
            stats["skipped"] += 1
            continue
        targets.append((key, urls))
    if not targets:
        return stats
    if dry_run:
        stats["skipped"] = len(targets)
        return stats

    token, err_kind, err_msg = _media_token()
    if err_kind:
        stats["failed"] = [(k, err_msg) for k, _ in targets]
        stats["auth_error"] = err_msg
        return stats

    for n, (key, urls) in enumerate(targets):
        if stats["auth_error"]:
            # Same token for every call — the remaining SKUs would fail identically.
            stats["failed"] += [(k, "skipped — eBay token rejected") for k, _ in targets[n:]]
            break
        gallery, expiries = [], []
        for i, url in enumerate(urls[:MAX_GALLERY]):
            try:
                jpeg = _download_as_jpeg(url)
            except Exception as e:
                stats["failed"].append((key, f"download {url[-60:]}: {e}"))
                continue
            if len(jpeg) > MAX_PHOTO_BYTES:
                stats["failed"].append((key, f"{len(jpeg) // 1024} KB exceeds 12 MB limit"))
                continue
            res = upload_picture(f"{key}_g{i}", jpeg, token)
            if res["ok"]:
                gallery.append(res["url"])
                if _parse_ts(res.get("expires_at")):
                    expiries.append(res["expires_at"])
                logger.info(f"✓ {key} gallery #{i + 1} -> {res['url']}")
            else:
                stats["failed"].append((key, res["message"]))
                logger.error(f"✗ {key} gallery #{i + 1}: {res['message']}")
                if res["error_kind"] == "auth":
                    stats["auth_error"] = res["message"]
                    logger.critical(f"upload_photos: eBay token rejected — stopping. {RECONSENT_HINT}")
                    break
        if gallery:
            entry = hosted.setdefault(key, {})
            entry["gallery"] = gallery
            if expiries:
                entry["gallery_expires_at"] = min(expiries, key=_parse_ts)   # earliest purge
            else:
                entry.pop("gallery_expires_at", None)
            save_hosted(hosted)
            stats["uploaded"] += len(gallery)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Upload data/listing_photos/*.jpg to eBay Picture Services")
    ap.add_argument("--dry-run", action="store_true", help="print what would be uploaded, no API calls")
    ap.add_argument("--force", action="store_true", help="re-upload every photo regardless of hash")
    ap.add_argument("--sku", help="upload only this SKU's photo")
    args = ap.parse_args(argv)

    stats = run(dry_run=args.dry_run, force=args.force, sku=args.sku)
    verb = "would upload" if args.dry_run else "uploaded"
    n = stats["planned"] if args.dry_run else stats["uploaded"]
    print(f"\nupload_photos: {verb} {n} / skipped {stats['skipped']} (unchanged) / "
          f"failed {len(stats['failed'])}")
    for s, msg in stats["failed"]:
        print(f"  FAILED {s}: {msg}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
