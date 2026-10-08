"""
Unit tests for tools/upload_photos.py — eBay POST is mocked, no live calls.
Run: python -m pytest tests/test_upload_photos.py -v
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tools import upload_photos as up

OK_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<UploadSiteHostedPicturesResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SiteHostedPictureDetails>
    <PictureName>SKU.jpg</PictureName>
    <FullURL>https://i.ebayimg.com/00/s/MTI4MFgxMjAw/z/SKU/img_1.JPG?set_id=2</FullURL>
  </SiteHostedPictureDetails>
</UploadSiteHostedPicturesResponse>"""

ERR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<UploadSiteHostedPicturesResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors><ShortMessage>Picture is too small.</ShortMessage><ErrorCode>CODE</ErrorCode>
  <SeverityCode>Error</SeverityCode></Errors>
</UploadSiteHostedPicturesResponse>"""


def _ok(sku):
    return OK_XML.replace(b"SKU", sku.encode())


def _err(code):
    return ERR_XML.replace(b"CODE", code.encode())


def _sku_of(body):
    return body.split("<PictureName>")[1].split(".jpg")[0]


@pytest.fixture
def env(monkeypatch, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    hosted = tmp_path / "hosted.json"
    monkeypatch.setattr(up, "_load_credentials",
                        lambda: ("TOKEN", "APP", "DEV", "CERT", None, ""))
    calls = []

    def fake_post(body, jpeg, headers):
        calls.append((body, headers, jpeg))
        return _ok(_sku_of(body))

    monkeypatch.setattr(up, "_post", fake_post)
    return photos, hosted, calls


def _photo(photos, sku, data=b"\xff\xd8jpegbytes"):
    (photos / f"{sku}.jpg").write_bytes(data)


def test_uploads_and_writes_map(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["uploaded"] == 1 and not stats["failed"]
    data = json.loads(hosted.read_text())
    assert data["X1"]["url"].startswith("https://i.ebayimg.com/")
    assert len(data["X1"]["sha256"]) == 64
    body, headers, jpeg = calls[0]
    assert headers["X-EBAY-API-CALL-NAME"] == "UploadSiteHostedPictures"
    assert "<PictureSystemVersion>2</PictureSystemVersion>" in body
    assert "<eBayAuthToken>TOKEN</eBayAuthToken>" in body
    assert "<PictureData>" not in body          # inline base64 is rejected by eBay (21916550)
    assert jpeg == b"\xff\xd8jpegbytes"
    assert not hosted.with_name(hosted.name + ".tmp").exists()


def test_skips_when_sha_unchanged_and_reuploads_when_changed(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    up.run(photos_dir=photos, hosted_path=hosted)
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


def test_stale_upload_is_refreshed(env):
    photos, hosted, calls = env
    _photo(photos, "X1")
    up.run(photos_dir=photos, hosted_path=hosted)
    data = json.loads(hosted.read_text())
    old = datetime.now(timezone.utc) - timedelta(days=up.REUPLOAD_AFTER_DAYS + 1)
    data["X1"]["uploaded_at"] = old.isoformat()
    hosted.write_text(json.dumps(data))
    up.run(photos_dir=photos, hosted_path=hosted)
    assert len(calls) == 2


def test_dry_run_makes_no_calls_and_no_writes(env, capsys):
    photos, hosted, calls = env
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
                        lambda b, j, h: _err("10007") if _sku_of(b) == "BAD" else _ok("GOOD"))
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["uploaded"] == 1
    assert stats["failed"] == [("BAD", "10007: Picture is too small.")]
    data = json.loads(hosted.read_text())
    assert "GOOD" in data and "BAD" not in data


def test_missing_full_url_is_a_failure(env, monkeypatch):
    photos, hosted, _ = env
    _photo(photos, "X1")
    monkeypatch.setattr(up, "_post", lambda b, j, h: b'<R xmlns="urn:ebay:apis:eBLBaseComponents">'
                                                 b"<Ack>Success</Ack></R>")
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["failed"] and "no FullURL" in stats["failed"][0][1]
    assert json.loads(hosted.read_text()) == {}


def test_network_error_does_not_crash(env, monkeypatch):
    photos, hosted, _ = env
    _photo(photos, "X1")

    def boom(b, j, h):
        raise OSError("connection reset")

    monkeypatch.setattr(up, "_post", boom)
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert stats["failed"][0][0] == "X1" and "connection reset" in stats["failed"][0][1]


def test_auth_error_stops_the_run(env, monkeypatch):
    photos, hosted, _ = env
    for s in ("A", "B", "C"):
        _photo(photos, s)
    calls = []

    def fake_post(b, j, h):
        calls.append(b)
        return _err("931")

    monkeypatch.setattr(up, "_post", fake_post)
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert len(calls) == 1 and len(stats["failed"]) == 3


def test_no_credentials_fails_without_calls(env, monkeypatch):
    photos, hosted, calls = env
    _photo(photos, "X1")
    monkeypatch.setattr(up, "_load_credentials",
                        lambda: ("", "", "", "", "no_token", "no EBAY_AUTH_TOKEN"))
    stats = up.run(photos_dir=photos, hosted_path=hosted)
    assert calls == [] and stats["failed"] == [("X1", "no EBAY_AUTH_TOKEN")]


def test_main_exit_codes(env, monkeypatch):
    photos, hosted, _ = env
    monkeypatch.setattr(up, "PHOTOS_DIR", photos)
    monkeypatch.setattr(up, "HOSTED_PATH", hosted)
    _photo(photos, "X1")
    assert up.main([]) == 0
    _photo(photos, "X2")
    monkeypatch.setattr(up, "_post", lambda b, j, h: _err("1"))
    assert up.main([]) == 1


def test_corrupt_map_starts_empty(tmp_path):
    p = tmp_path / "hosted.json"
    p.write_text("{not json")
    assert up.load_hosted(p) == {}


def test_multipart_body_carries_xml_and_raw_jpeg():
    body = up._build_multipart("<X/>", b"\xff\xd8\x00raw", "BND")
    assert body.startswith(b"--BND\r\n") and body.endswith(b"--BND--\r\n")
    assert b'name="XML Payload"' in body and b"<X/>" in body
    assert b"\r\n\r\n\xff\xd8\x00raw\r\n--BND--" in body


def test_hosted_url_for():
    assert up.hosted_url_for("X1", {"X1": {"url": "https://i.ebayimg.com/a.jpg"}}) == "https://i.ebayimg.com/a.jpg"
    assert up.hosted_url_for("X1", {}) is None
    assert up.hosted_url_for("", {"": {"url": "https://x"}}) is None
    assert up.hosted_url_for("X1", {"X1": {"url": "not a url"}}) is None
