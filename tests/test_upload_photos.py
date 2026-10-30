"""
Unit tests for tools/upload_photos.py — eBay Media API POST + token are mocked, no live calls.
Run: python -m pytest tests/test_upload_photos.py -v
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tools import ebay_analytics
from tools import upload_photos as up

EXPIRES = "2026-12-01T00:00:00.000Z"


def _ok(sku):
    return 201, {"imageUrl": f"https://i.ebayimg.com/images/g/{sku}/s-l1600.jpg",
                 "expirationDate": EXPIRES}, {"Location": f"https://apim.ebay.com/x/image/{sku}"}


def _err(status, msg="Picture is too small.", error_id=190202):
    return status, {"errors": [{"errorId": error_id, "message": msg}]}, {}


def _sku_of(filename):
    return filename.rsplit(".jpg", 1)[0]


@pytest.fixture
def env(monkeypatch, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    hosted = tmp_path / "hosted.json"
    monkeypatch.setattr(up, "_media_token", lambda: ("TOKEN", None, ""))
    monkeypatch.setattr(up, "_sleep", lambda s: None)
    calls = []

    def fake_post(jpeg, filename, token):
        calls.append((filename, token, jpeg))
        return _ok(_sku_of(filename))

    monkeypatch.setattr(up, "_post", fake_post)
    return photos, hosted, calls


def _photo(photos, sku, data=b"\xff\xd8jpegbytes"):
    (photos / f"{sku}.jpg").write_bytes(data)


def _age(hosted, sku, **fields):
    data = json.loads(hosted.read_text())
    data[sku].update(fields)
    hosted.write_text(json.dumps(data))


def test_uploads_and_writes_map_with_expiry(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["uploaded"] == 1 and not stats["failed"] and stats["auth_error"] == ""
    data = json.loads(hosted.read_text())
    assert data["X1"]["url"].startswith("https://i.ebayimg.com/")
    assert data["X1"]["expires_at"] == EXPIRES
    assert len(data["X1"]["sha256"]) == 64
    assert calls == [("X1.jpg", "TOKEN", b"\xff\xd8jpegbytes")]
    assert not hosted.with_name(hosted.name + ".tmp").exists()


def test_skips_when_sha_unchanged_and_reuploads_when_changed(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    up.run(photos_dir=photos, hosted_path=hosted)
    _age(hosted, "X1", expires_at=(datetime.now(timezone.utc) + timedelta(days=20)).isoformat())
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert len(calls) == 1 and stats["skipped"] == 1 and stats["uploaded"] == 0
    _photo(photos, "X1", b"\xff\xd8new render")
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert len(calls) == 2 and stats["uploaded"] == 1


def test_force_reuploads(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    up.run(photos_dir=photos, hosted_path=hosted)
    up.run(photos_dir=photos, hosted_path=hosted, force=True)
    assert len(calls) == 2


def test_stale_upload_without_expiry_is_refreshed(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    up.run(photos_dir=photos, hosted_path=hosted)
    old = datetime.now(timezone.utc) - timedelta(days=up.REUPLOAD_AFTER_DAYS + 1)
    data = json.loads(hosted.read_text())
    data["X1"].pop("expires_at")
    data["X1"]["uploaded_at"] = old.isoformat()
    hosted.write_text(json.dumps(data))
    up.run(photos_dir=photos, hosted_path=hosted)
    assert len(calls) == 2


def test_entry_near_its_expiration_date_is_refreshed(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    up.run(photos_dir=photos, hosted_path=hosted)
    soon = datetime.now(timezone.utc) + timedelta(days=up.EXPIRY_MARGIN_DAYS - 1)
    _age(hosted, "X1", expires_at=soon.isoformat())
    up.run(photos_dir=photos, hosted_path=hosted)
    assert len(calls) == 2


def test_failed_reupload_of_a_stale_entry_drops_the_dead_url(env, monkeypatch):
    photos, hosted, calls = env
    _photo(photos, "OLD")
    _photo(photos, "CHANGED")
    up.run(photos_dir=photos, hosted_path=hosted)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _age(hosted, "OLD", expires_at=past)                       # purged by eBay
    _photo(photos, "CHANGED", b"\xff\xd8re-render")            # fresh entry, new bytes
    monkeypatch.setattr(up, "_post", lambda j, f, t: _err(404, "Resource not found"))
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    data = json.loads(hosted.read_text())
    assert stats["dropped"] == ["OLD"] and "OLD" not in data
    assert "CHANGED" in data                                   # still serves the old render


def test_dry_run_makes_no_calls_and_no_writes(env, capsys, monkeypatch):
    photos, hosted, calls = env
    monkeypatch.setattr(up, "_media_token", lambda: pytest.fail("dry run must not mint a token"))
    _photo(photos, "X1")
    stats = up.run(photos_dir=photos, hosted_path=hosted, dry_run=True)
    assert calls == [] and stats["planned"] == 1
    assert not hosted.exists()
    assert "would upload X1" in capsys.readouterr().out


def test_sku_filter(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    _photo(photos, "X2")
    up.run(photos_dir=photos, hosted_path=hosted, sku="X2")
    assert [_sku_of(c[0]) for c in calls] == ["X2"]


def test_error_recorded_others_continue(env, monkeypatch):
    photos, hosted, _ = env
    _photo(photos, "BAD")
    _photo(photos, "GOOD")
    monkeypatch.setattr(up, "_post",
                        lambda j, f, t: _err(400) if _sku_of(f) == "BAD" else _ok("GOOD"))
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["uploaded"] == 1
    assert stats["failed"] == [("BAD", "HTTP 400 190202: Picture is too small.")]
    data = json.loads(hosted.read_text())
    assert "GOOD" in data and "BAD" not in data


def test_missing_image_url_falls_back_to_get_image_via_location(env, monkeypatch):
    photos, hosted, _ = env
    _photo(photos, "X1")
    monkeypatch.setattr(up, "_post", lambda j, f, t: (201, {}, {"location": "https://apim/x/image/1"}))
    got = []
    monkeypatch.setattr(up, "_get_image", lambda loc, t: got.append(loc) or _ok("X1"))
    assert up.run(photos_dir=photos, hosted_path=hosted)["uploaded"] == 1
    assert got == ["https://apim/x/image/1"]


def test_missing_image_url_is_a_failure(env, monkeypatch):
    photos, hosted, _ = env
    _photo(photos, "X1")
    monkeypatch.setattr(up, "_post", lambda j, f, t: (201, {}, {}))
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["failed"] and "no imageUrl" in stats["failed"][0][1]
    assert json.loads(hosted.read_text()) == {}


def test_plain_text_404_is_reported_verbatim(env, monkeypatch):
    # what the retired Trading call answered: 404 text/xml "Resource not found"
    photos, hosted, _ = env
    _photo(photos, "X1")
    monkeypatch.setattr(up, "_post", lambda j, f, t: (404, {"_raw": "Resource not found"}, {}))
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["failed"] == [("X1", "HTTP 404: Resource not found")]


def test_network_error_does_not_crash(env, monkeypatch):
    photos, hosted, _ = env
    _photo(photos, "X1")

    def boom(j, f, t):
        raise OSError("connection reset")

    monkeypatch.setattr(up, "_post", boom)
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["failed"][0][0] == "X1" and "connection reset" in stats["failed"][0][1]


def test_auth_error_stops_the_run_with_a_renew_hint(env, monkeypatch):
    photos, hosted, _ = env
    for s in ("A", "B", "C"):
        _photo(photos, s)
    calls = []

    def fake_post(j, f, t):
        calls.append(f)
        return _err(403, "Unauthorized access.", 190013)

    monkeypatch.setattr(up, "_post", fake_post)
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert len(calls) == 1 and len(stats["failed"]) == 3
    assert "sell.inventory" in stats["auth_error"] and "EBAY_OAUTH_REFRESH_TOKEN" in stats["auth_error"]


def test_no_token_fails_without_calls(env, monkeypatch):
    photos, hosted, calls = env
    _photo(photos, "X1")
    monkeypatch.setattr(up, "_media_token", lambda: (None, "auth", "no Media API token"))
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert calls == [] and stats["failed"] == [("X1", "no Media API token")]
    assert stats["auth_error"] == "no Media API token"


def test_media_token_explains_a_missing_scope(monkeypatch, tmp_path):
    seen = {}

    def fake_token(force=False, scopes=None, cache_path=None):
        seen.update(scopes=scopes, cache_path=cache_path)
        ebay_analytics.last_token_error = "HTTP 400: The requested scope is invalid"
        return None

    monkeypatch.setattr(ebay_analytics, "get_user_access_token", fake_token)
    token, kind, msg = up._media_token()
    assert token is None and kind == "auth"
    assert "scope is invalid" in msg and "sell.inventory" in msg
    assert seen["scopes"] == up.MEDIA_SCOPES and seen["cache_path"] == up.MEDIA_TOKEN_PATH


def test_main_exit_codes(env, monkeypatch):
    photos, hosted, _ = env
    monkeypatch.setattr(up, "PHOTOS_DIR", photos)
    monkeypatch.setattr(up, "HOSTED_PATH", hosted)
    _photo(photos, "X1")
    assert up.main([]) == 0
    _photo(photos, "X2")
    monkeypatch.setattr(up, "_post", lambda j, f, t: _err(400))
    assert up.main([]) == 1


def test_corrupt_map_starts_empty(tmp_path):
    p = tmp_path / "hosted.json"
    p.write_text("{not json")
    assert up.load_hosted(p) == {}


def test_multipart_body_is_one_image_part_with_raw_jpeg():
    body = up._build_multipart(b"\xff\xd8\x00raw", "X 1.jpg", "BND")
    assert body.startswith(b"--BND\r\n") and body.endswith(b"--BND--\r\n")
    assert b'name="image"; filename="X_1.jpg"' in body and b"Content-Type: image/jpeg" in body
    assert b"XML Payload" not in body
    assert b"\r\n\r\n\xff\xd8\x00raw\r\n--BND--" in body


def test_hosted_url_for():
    assert up.hosted_url_for("X1", {"X1": {"url": "https://i.ebayimg.com/a.jpg"}}) == "https://i.ebayimg.com/a.jpg"
    assert up.hosted_url_for("X1", {}) is None
    assert up.hosted_url_for("", {"": {"url": "https://x"}}) is None
    assert up.hosted_url_for("X1", {"X1": {"url": "not a url"}}) is None


def test_hosted_url_for_ignores_an_expired_picture():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    entry = {"url": "https://i.ebayimg.com/a.jpg", "expires_at": "2026-10-31T00:00:00.000Z"}
    assert up.hosted_url_for("X1", {"X1": entry}, now=now) is None
    entry["expires_at"] = "2026-11-20T00:00:00.000Z"
    assert up.hosted_url_for("X1", {"X1": entry}, now=now) == "https://i.ebayimg.com/a.jpg"


# ── Gallery (col AT → EPS) ────────────────────────────────────────────────────

def test_safe_sku_matches_photo_compositor():
    assert up._safe_sku("A/B C") == "A_B_C"
    assert up._safe_sku("X0bac7f5423") == "X0bac7f5423"


def test_upload_gallery_hosts_col_at_shots(env, monkeypatch):
    photos, hosted, calls = env
    monkeypatch.setattr(up, "HOSTED_PATH", hosted)
    up.save_hosted({"X1": {"url": "https://i.ebayimg.com/branded.jpg", "sha256": "abc"}}, hosted)
    monkeypatch.setattr(up, "_download_as_jpeg", lambda url: b"\xff\xd8gallery")
    jobs = [{"sku": "X1", "urls": ["https://a.com/1.jpg", "https://a.com/2.jpg"]}]

    stats = up.upload_gallery(jobs)
    assert stats["uploaded"] == 2 and not stats["failed"]
    data = json.loads(hosted.read_text())
    gallery = data["X1"]["gallery"]
    assert len(gallery) == 2 and all(u.startswith("https://i.ebayimg.com/") for u in gallery)
    assert data["X1"]["gallery_expires_at"] == EXPIRES
    assert [_sku_of(c[0]) for c in calls] == ["X1_g0", "X1_g1"]


def test_upload_gallery_skips_when_cached(env, monkeypatch):
    photos, hosted, calls = env
    monkeypatch.setattr(up, "HOSTED_PATH", hosted)
    up.save_hosted({"X1": {"url": "https://i.ebayimg.com/branded.jpg",
                           "gallery": ["https://i.ebayimg.com/g0.jpg"]}}, hosted)
    monkeypatch.setattr(up, "_download_as_jpeg", lambda url: b"\xff\xd8")
    stats = up.upload_gallery([{"sku": "X1", "urls": ["https://a.com/1.jpg"]}])
    assert calls == [] and stats["skipped"] == 1 and stats["uploaded"] == 0


def test_upload_gallery_rehosts_a_gallery_near_expiry(env, monkeypatch):
    photos, hosted, calls = env
    monkeypatch.setattr(up, "HOSTED_PATH", hosted)
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    up.save_hosted({"X1": {"url": "https://i.ebayimg.com/branded.jpg",
                           "gallery": ["https://i.ebayimg.com/g0.jpg"],
                           "gallery_expires_at": soon}}, hosted)
    monkeypatch.setattr(up, "_download_as_jpeg", lambda url: b"\xff\xd8")
    assert up.upload_gallery([{"sku": "X1", "urls": ["https://a.com/1.jpg"]}])["uploaded"] == 1


def test_upload_gallery_auth_error_stops_every_sku(env, monkeypatch):
    photos, hosted, calls = env
    monkeypatch.setattr(up, "HOSTED_PATH", hosted)
    up.save_hosted({k: {"url": f"https://i.ebayimg.com/{k}.jpg"} for k in ("A", "B")}, hosted)
    monkeypatch.setattr(up, "_download_as_jpeg", lambda url: b"\xff\xd8")
    posts = []
    monkeypatch.setattr(up, "_post", lambda j, f, t: posts.append(f) or _err(401, "Invalid access token", 1001))
    stats = up.upload_gallery([{"sku": "A", "urls": ["https://a.com/1.jpg", "https://a.com/2.jpg"]},
                               {"sku": "B", "urls": ["https://a.com/3.jpg"]}])
    assert posts == ["A_g0.jpg"]
    assert [k for k, _ in stats["failed"]] == ["A", "B"] and "sell.inventory" in stats["auth_error"]


def test_upload_gallery_ignores_sku_without_branded_photo(env, monkeypatch):
    photos, hosted, calls = env
    monkeypatch.setattr(up, "HOSTED_PATH", hosted)
    up.save_hosted({"OTHER": {"url": "https://i.ebayimg.com/other.jpg"}}, hosted)
    monkeypatch.setattr(up, "_download_as_jpeg", lambda url: b"\xff\xd8")
    stats = up.upload_gallery([{"sku": "X1", "urls": ["https://a.com/1.jpg"]}])
    assert calls == [] and stats["uploaded"] == 0 and stats["skipped"] == 0


def test_upload_gallery_dry_run_makes_no_calls(env, monkeypatch):
    photos, hosted, calls = env
    monkeypatch.setattr(up, "HOSTED_PATH", hosted)
    up.save_hosted({"X1": {"url": "https://i.ebayimg.com/branded.jpg"}}, hosted)
    monkeypatch.setattr(up, "_download_as_jpeg", lambda url: b"\xff\xd8")
    stats = up.upload_gallery([{"sku": "X1", "urls": ["https://a.com/1.jpg"]}], dry_run=True)
    assert calls == [] and stats["skipped"] == 1
    assert "gallery" not in json.loads(hosted.read_text())["X1"]
