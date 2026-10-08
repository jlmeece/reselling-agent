"""
upload_photos.py — Host the branded listing photos (tools/photo_compositor.py) on eBay.

For every data/listing_photos/<sku>.jpg, uploads the JPEG with the Trading API's
UploadSiteHostedPictures (eBay Picture Services, JPEG sent as a multipart binary
attachment) and records the hosted URL in
data/hosted_photos.json:

    {"<sku>": {"url": "https://i.ebayimg.com/...", "sha256": "...", "uploaded_at": "ISO"}}

tools/ebay_export.py reads that map and puts the hosted URL FIRST in PicURL (the main
image), followed by the raw Costco shots from col AT.

A photo is skipped when its sha256 matches the stored one (unchanged since upload) and
the upload is younger than REUPLOAD_AFTER_DAYS — eBay purges hosted pictures that never
get attached to a listing, so an old URL for a not-yet-listed photo may be dead.

Usage:
  python tools/upload_photos.py              # upload new / changed photos
  python tools/upload_photos.py --dry-run    # show what would be uploaded, no API calls
  python tools/upload_photos.py --force      # re-upload everything regardless of hash
  python tools/upload_photos.py --sku X1     # just this SKU

Exit code: 0 = nothing failed, 1 = at least one upload failed (or no credentials).
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import socket
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from tools.ebay_sync import (  # noqa: E402 — reuse the Trading API plumbing
    AUTH_ERROR_CODES, ENDPOINT, REQUEST_TIMEOUT, _NS, _child, _headers, _load_credentials,
    _parse_errors, _sleep, _text, _xml_escape,
)

ROOT        = Path(__file__).resolve().parent.parent
PHOTOS_DIR  = ROOT / "data" / "listing_photos"
HOSTED_PATH = ROOT / "data" / "hosted_photos.json"
REUPLOAD_AFTER_DAYS = 25     # refresh hosted URLs older than this (unlisted pictures get purged)
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


def hosted_url_for(sku: str, hosted: dict) -> str | None:
    """The hosted URL for a SKU, or None. Used by ebay_export."""
    entry = hosted.get(sku) if sku else None
    url = entry.get("url") if isinstance(entry, dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


# ── Upload ────────────────────────────────────────────────────────────────────

def _build_upload_xml(token: str, sku: str) -> str:
    """Request XML WITHOUT <PictureData>: eBay rejects an inline base64 picture
    (live 2026-10-07: error 21916550 "File has corrupt image data") — the bytes must
    travel as a binary MIME attachment, see _build_multipart."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<UploadSiteHostedPicturesRequest xmlns="{_NS}">'
        f"<RequesterCredentials><eBayAuthToken>{_xml_escape(token)}</eBayAuthToken></RequesterCredentials>"
        f"<PictureName>{_xml_escape(sku)}.jpg</PictureName>"
        "<PictureSystemVersion>2</PictureSystemVersion>"
        "</UploadSiteHostedPicturesRequest>"
    )


def _build_multipart(xml: str, jpeg: bytes, boundary: str) -> bytes:
    """multipart/form-data body: the XML payload part, then the JPEG as a binary part."""
    crlf, b = b"\r\n", boundary.encode()
    return b"".join([
        b"--" + b + crlf,
        b'Content-Disposition: form-data; name="XML Payload"' + crlf,
        b"Content-Type: text/xml;charset=utf-8" + crlf + crlf,
        xml.encode("utf-8") + crlf,
        b"--" + b + crlf,
        b'Content-Disposition: form-data; name="image"; filename="image.jpg"' + crlf,
        b"Content-Transfer-Encoding: binary" + crlf,
        b"Content-Type: application/octet-stream" + crlf + crlf,
        jpeg + crlf,
        b"--" + b + b"--" + crlf,
    ])


def _post(xml: str, jpeg: bytes, headers: dict) -> bytes:
    """POST the multipart upload to the Trading API endpoint; one retry on timeout /
    connection error / HTTP 5xx (same policy as ebay_sync._post, which only sends XML).
    Raises OSError if both attempts fail."""
    boundary = f"MIME_boundary_{uuid.uuid4().hex}"
    hdrs = dict(headers, **{"Content-Type": f"multipart/form-data; boundary={boundary}"})
    req = urllib.request.Request(ENDPOINT, data=_build_multipart(xml, jpeg, boundary),
                                 headers=hdrs, method="POST")
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT * 2) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
            logger.warning(f"upload_photos: HTTP {e.code} from eBay — retrying once")
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            if attempt == 2:
                raise
            logger.warning(f"upload_photos: eBay request failed ({e}) — retrying once")
        _sleep(2)


def upload_picture(sku: str, jpeg: bytes, creds: tuple) -> dict:
    """POST one picture. Returns {ok, url, error_kind, message}. Never raises.
    error_kind: None | "auth" (931/932/16110) | "api" | "network"."""
    token, app_id, dev_id, cert_id = creds
    try:
        raw = _post(_build_upload_xml(token, sku), jpeg,
                    _headers(app_id, dev_id, cert_id, "UploadSiteHostedPictures"))
    except OSError as e:
        return {"ok": False, "url": None, "error_kind": "network", "message": f"eBay request failed: {e}"}
    except Exception as e:  # contract: never raises
        return {"ok": False, "url": None, "error_kind": "api", "message": f"unexpected error: {e}"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return {"ok": False, "url": None, "error_kind": "api", "message": f"unparseable eBay response: {e}"}
    ack = _text(root, "Ack")
    details = _child(root, "SiteHostedPictureDetails")
    url = _text(details, "FullURL") if details is not None else ""
    if ack in ("Success", "Warning") and url:
        return {"ok": True, "url": url, "error_kind": None, "message": ""}
    errors = _parse_errors(root)
    codes = [c for c, _ in errors]
    msg = "; ".join(f"{c}: {m}" for c, m in errors) or (
        f"Ack={ack or 'missing'}" + ("" if url else ", no FullURL in response"))
    kind = "auth" if any(c in AUTH_ERROR_CODES for c in codes) else "api"
    return {"ok": False, "url": None, "error_kind": kind, "message": msg}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_fresh(entry: dict, now: datetime) -> bool:
    """Uploaded within REUPLOAD_AFTER_DAYS. A missing/bad timestamp counts as fresh
    (older map entries without one are not re-uploaded en masse)."""
    try:
        ts = datetime.fromisoformat(entry["uploaded_at"])
    except (KeyError, TypeError, ValueError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
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


def run(*, dry_run=False, force=False, sku=None, photos_dir=None, hosted_path=None) -> dict:
    """Returns {uploaded, skipped, failed: [(sku, msg)], planned}."""
    photos_dir = Path(photos_dir or PHOTOS_DIR)
    hosted_path = Path(hosted_path or HOSTED_PATH)
    stats = {"uploaded": 0, "skipped": 0, "failed": [], "planned": 0}

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

    token, app_id, dev_id, cert_id, err_kind, err_msg = _load_credentials()
    if err_kind:
        logger.error(f"upload_photos: {err_msg} — nothing uploaded")
        stats["failed"] = [(s, err_msg) for s, *_ in todo]
        return stats
    creds = (token, app_id, dev_id, cert_id)

    for i, (s, p, data, sha, reason) in enumerate(todo):
        if len(data) > MAX_PHOTO_BYTES:
            res = {"ok": False, "error_kind": "invalid",
                   "message": f"{len(data) // 1024} KB exceeds eBay's 12 MB picture limit"}
        else:
            res = upload_picture(s, data, creds)
        if res["ok"]:
            hosted[s] = {"url": res["url"], "sha256": sha,
                         "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
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
                logger.critical("upload_photos: eBay token rejected — stopping. Renew EBAY_AUTH_TOKEN.")
                break
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
    Returns {uploaded, skipped, failed: [(sku, msg)]}. Never raises."""
    stats = {"uploaded": 0, "skipped": 0, "failed": []}
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
        if entry.get("gallery") and not force:
            stats["skipped"] += 1
            continue
        targets.append((key, urls))
    if not targets:
        return stats
    if dry_run:
        stats["skipped"] = len(targets)
        return stats

    token, app_id, dev_id, cert_id, err_kind, err_msg = _load_credentials()
    if err_kind:
        stats["failed"] = [(k, err_msg) for k, _ in targets]
        return stats
    creds = (token, app_id, dev_id, cert_id)

    for key, urls in targets:
        gallery = []
        for i, url in enumerate(urls[:MAX_GALLERY]):
            try:
                jpeg = _download_as_jpeg(url)
            except Exception as e:
                stats["failed"].append((key, f"download {url[-60:]}: {e}"))
                continue
            if len(jpeg) > MAX_PHOTO_BYTES:
                stats["failed"].append((key, f"{len(jpeg) // 1024} KB exceeds 12 MB limit"))
                continue
            res = upload_picture(f"{key}_g{i}", jpeg, creds)
            if res["ok"]:
                gallery.append(res["url"])
                logger.info(f"✓ {key} gallery #{i + 1} -> {res['url']}")
            else:
                stats["failed"].append((key, res["message"]))
                if res["error_kind"] == "auth":
                    break
        if gallery:
            entry = hosted.setdefault(key, {})
            entry["gallery"] = gallery
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
