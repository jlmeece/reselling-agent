"""tools/ebay_consent.py — eBay token endpoint mocked, .env is a temp file, never live."""
import os
import sys
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import ebay_analytics
from tools import ebay_consent as ec

REFRESH = "v^1.1#i^1#r^1#f^0#I^3#t^Ul4xMF8w"


def test_consent_url_asks_for_every_scope_the_tool_uses():
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(ec.consent_url("APP", "my-runame")).query)
    assert q["client_id"] == ["APP"] and q["redirect_uri"] == ["my-runame"]
    assert q["response_type"] == ["code"]
    scopes = q["scope"][0].split()
    assert "https://api.ebay.com/oauth/api_scope" in scopes
    assert "https://api.ebay.com/oauth/api_scope/sell.inventory" in scopes
    assert set(ebay_analytics.SCOPES) <= set(scopes) and len(scopes) == len(set(scopes))


@pytest.mark.parametrize("pasted", [
    "https://auth2.ebay.com/oauth2/ThirdPartyAuthSucessFailure?isAuthSuccessful=true&code=v%5E1.1%23i%5E1%23abc&expires_in=299",
    "https://example.com/accepted?state=x&code=v%5E1.1%23i%5E1%23abc",
    '"https://example.com/accepted?code=v%5E1.1%23i%5E1%23abc"',
    "v%5E1.1%23i%5E1%23abc",
    "  v^1.1#i^1#abc  ",
])
def test_extract_code_from_landing_url_or_bare_code(pasted):
    assert ec.extract_code(pasted) == "v^1.1#i^1#abc"


def test_extract_code_without_a_code_is_empty():
    assert ec.extract_code("https://example.com/declined?isAuthSuccessful=false") == ""


def test_exchange_code_sends_authorization_code_grant(monkeypatch):
    seen = {}

    def fake(req):
        seen["body"] = urllib.parse.parse_qs(req.data.decode())
        seen["auth"] = req.headers.get("Authorization")
        return 200, {"access_token": "ACCESS", "refresh_token": REFRESH,
                     "refresh_token_expires_in": 47304000}
    monkeypatch.setattr(ebay_analytics, "_http_json", fake)
    res = ec.exchange_code("CODE", "APP", "CERT", "my-runame")
    assert res == {"ok": True, "refresh_token": REFRESH, "refresh_expires_in": 47304000, "message": ""}
    assert seen["body"] == {"grant_type": ["authorization_code"], "code": ["CODE"],
                            "redirect_uri": ["my-runame"]}
    assert seen["auth"].startswith("Basic ")


def test_exchange_code_failure_is_reported(monkeypatch):
    monkeypatch.setattr(ebay_analytics, "_http_json", lambda req: (
        400, {"error": "invalid_grant", "error_description": "the provided authorization grant code is invalid"}))
    res = ec.exchange_code("OLD", "APP", "CERT", "r")
    assert not res["ok"] and "grant code is invalid" in res["message"]


def test_set_env_value_replaces_quoted_and_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nEBAY_OAUTH_REFRESH_TOKEN=\"old#value\"\n# comment\nB=2", encoding="utf-8")
    ec.set_env_value(env, "EBAY_OAUTH_REFRESH_TOKEN", REFRESH)
    ec.set_env_value(env, "EBAY_RUNAME", "my-runame")
    assert env.read_text(encoding="utf-8") == (
        f'A=1\nEBAY_OAUTH_REFRESH_TOKEN="{REFRESH}"\n# comment\nB=2\nEBAY_RUNAME="my-runame"\n')
    from dotenv import dotenv_values
    assert dotenv_values(env)["EBAY_OAUTH_REFRESH_TOKEN"] == REFRESH     # '#' survives


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text('EBAY_APP_ID=APP\nEBAY_CERT_ID=CERT\nEBAY_OAUTH_REFRESH_TOKEN="access-token-by-mistake"\n',
                   encoding="utf-8")
    for k in ("EBAY_APP_ID", "EBAY_CERT_ID", "EBAY_OAUTH_REFRESH_TOKEN", "EBAY_RUNAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ec, "ENV_PATH", env)
    monkeypatch.setattr(ec, "ROOT", tmp_path)
    monkeypatch.setattr(ec, "BACKUP_DIR", tmp_path / ".tmp")
    monkeypatch.setattr(ec.webbrowser, "open", lambda url: pytest.fail("must not open a browser"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".ebay_user_token.json").write_text("{}")
    grants = []

    def fake(req):
        body = urllib.parse.parse_qs(req.data.decode())
        grants.append(body["grant_type"][0])
        if body["grant_type"] == ["authorization_code"]:
            return 200, {"access_token": "A", "refresh_token": REFRESH, "refresh_token_expires_in": 47304000}
        ok = body["refresh_token"] == [REFRESH]
        return (200, {"access_token": "A", "expires_in": 7200}) if ok else (400, {"error": "invalid_grant"})
    monkeypatch.setattr(ebay_analytics, "_http_json", fake)
    return env, tmp_path, grants


def test_full_run_writes_refresh_token_backs_up_and_verifies(sandbox, capsys):
    env, root, grants = sandbox
    rc = ec.main(["--runame", "my-runame", "--code",
                  "https://auth2.ebay.com/x?isAuthSuccessful=true&code=v%5E1.1%23abc"])
    out = capsys.readouterr().out
    assert rc == 0, out
    from dotenv import dotenv_values
    vals = dotenv_values(env)
    assert vals["EBAY_OAUTH_REFRESH_TOKEN"] == REFRESH and vals["EBAY_RUNAME"] == "my-runame"
    assert grants == ["authorization_code"] + ["refresh_token"] * len(ec.SCOPE_SETS)
    backups = list((root / ".tmp").glob(".env.backup-*"))
    assert len(backups) == 1 and "access-token-by-mistake" in backups[0].read_text()
    assert not (root / "data" / ".ebay_user_token.json").exists()       # stale cache cleared
    assert out.count("OK  ") == len(ec.SCOPE_SETS) and REFRESH not in out


def test_dry_run_prints_url_and_writes_nothing(sandbox, capsys):
    env, root, grants = sandbox
    before = env.read_text()
    assert ec.main(["--runame", "my-runame", "--dry-run"]) == 0
    assert "auth.ebay.com/oauth2/authorize" in capsys.readouterr().out
    assert env.read_text() == before and grants == []


def test_failed_swap_leaves_env_untouched(sandbox, monkeypatch):
    env, root, grants = sandbox
    monkeypatch.setattr(ebay_analytics, "_http_json", lambda req: (400, {"error": "invalid_grant"}))
    before = env.read_text()
    assert ec.main(["--code", "code=x", "--no-browser"]) == 1          # no RuName at all
    assert ec.main(["--runame", "r", "--code", "https://x/?code=expired", "--no-browser"]) == 1
    from dotenv import dotenv_values
    assert dotenv_values(env)["EBAY_OAUTH_REFRESH_TOKEN"] == "access-token-by-mistake"
