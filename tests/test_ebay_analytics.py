"""tools/ebay_analytics.py — user OAuth token + Sell Analytics traffic report (HTTP mocked)."""
import base64
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import ebay_analytics as ea

REPORT = {"header": {"dimensionKeys": [{"key": "LISTING"}], "metrics": [{"key": "LISTING_VIEWS_TOTAL"}]},
          "records": [
              {"dimensionValues": [{"value": "860016681733"}], "metricValues": [{"value": 141}]},
              {"dimensionValues": [{"value": "318943101194"}], "metricValues": [{"value": 41}]},
              {"dimensionValues": [{"value": ""}], "metricValues": [{"value": 9}]},          # skipped
              {"dimensionValues": [{"value": "111"}], "metricValues": []},                   # skipped
          ]}


@pytest.fixture
def http(monkeypatch):
    st = {"reqs": [], "token_status": 200, "report_status": 200, "report": REPORT, "n": 0}
    monkeypatch.setenv("EBAY_OAUTH_REFRESH_TOKEN", "v^1.1#refresh")
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")

    def fake(req):
        st["reqs"].append(req)
        if req.full_url == ea.TOKEN_URL:
            st["n"] += 1
            if st["token_status"] != 200:
                return st["token_status"], {"error": "invalid_grant",
                                            "error_description": "the provided authorization refresh token is invalid"}
            return 200, {"access_token": f"tok{st['n']}", "expires_in": 7200, "token_type": "User Access Token"}
        if st["report_status"] != 200:
            return st["report_status"], {"errors": [{"errorId": 1100, "message": "Access denied"}]}
        return 200, st["report"]
    monkeypatch.setattr(ea, "_http_json", fake)
    return st


def test_token_request_uses_basic_auth_and_refresh_grant(http):
    assert ea.get_user_access_token() == "tok1"
    req = http["reqs"][0]
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"app:cert").decode()
    form = urllib.parse.parse_qs(req.data.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["v^1.1#refresh"]
    assert form["scope"][0].split() == list(ea.SCOPES)


def test_pasted_token_response_json_is_accepted(http, monkeypatch):
    monkeypatch.setenv("EBAY_OAUTH_REFRESH_TOKEN",
                       json.dumps({"access_token": "old", "refresh_token": "v^1.1#inner", "expires_in": 7200}))
    assert ea.get_user_access_token() == "tok1"
    assert urllib.parse.parse_qs(http["reqs"][0].data.decode())["refresh_token"] == ["v^1.1#inner"]


def test_token_cached_until_near_expiry_then_refreshed(http, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(ea, "_now", lambda: now[0])
    assert ea.get_user_access_token() == "tok1"
    now[0] += 7200 - 61                              # still inside (ttl - 60s skew)
    assert ea.get_user_access_token() == "tok1" and http["n"] == 1
    now[0] += 2                                      # past expires_at
    assert ea.get_user_access_token() == "tok2" and http["n"] == 2
    assert ea.get_user_access_token(force=True) == "tok3"


def test_token_failure_returns_none(http):
    http["token_status"] = 400
    assert ea.get_user_access_token() is None


def test_missing_env_returns_none_without_http(http, monkeypatch):
    monkeypatch.delenv("EBAY_OAUTH_REFRESH_TOKEN")
    assert ea.get_user_access_token() is None and http["reqs"] == []


def test_get_listing_views_parses_records(http):
    views = ea.get_listing_views(30)
    assert views == {"860016681733": 141, "318943101194": 41}
    req = http["reqs"][-1]
    assert req.get_header("Authorization") == "Bearer tok1"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
    assert q["dimension"] == ["LISTING"] and q["metric"] == ["LISTING_VIEWS_TOTAL"]
    assert q["filter"][0].startswith("marketplace_ids:{EBAY_US},date_range:[")


@pytest.mark.parametrize("change", [{"report_status": 403}, {"report": {"records": []}},
                                    {"report": {"oops": 1}}, {"token_status": 401}])
def test_get_listing_views_none_on_bad_response(http, change):
    http.update(change)
    assert ea.get_listing_views() is None


def test_refresh_views_cache_writes_then_reuses_for_24h(http):
    assert ea.refresh_views_cache() == "views refreshed (2)"
    assert ea.cached_views("860016681733") == 141 and ea.cached_views("999") is None
    calls = len(http["reqs"])
    assert ea.refresh_views_cache() == "views cached (2)" and len(http["reqs"]) == calls


def test_refresh_views_cache_refetches_when_stale(http):
    ea._write_json(ea.VIEWS_PATH, {"fetched_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
                                   "days": 30, "views": {"1": 5}})
    assert ea.refresh_views_cache() == "views refreshed (2)"
    assert ea.cached_views("1") is None


def test_failed_refresh_keeps_stale_cache(http):
    ea._write_json(ea.VIEWS_PATH, {"fetched_at": "2026-01-01T00:00:00+00:00", "days": 30, "views": {"1": 5}})
    http["report_status"] = 500
    assert ea.refresh_views_cache() == "views fetch failed (kept cache)"
    assert ea.cached_views("1") == 5


def test_cached_views_without_file_is_none():
    assert ea.cached_views("860016681733") is None
