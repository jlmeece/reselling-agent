"""
ebay_consent.py — get a NEW eBay OAuth refresh token and write it to .env, no hand-picking.

eBay's sign-in hands back two tokens: a ~2-hour ACCESS token and an ~18-month REFRESH
token. Copying one by hand went wrong twice (2026-11-01: the access token landed in
EBAY_OAUTH_REFRESH_TOKEN, so every refresh grant failed "refresh token is invalid or was
issued to another client"). This script does the whole swap itself:

  1. prints (and opens) eBay's consent URL for every scope the tool uses
  2. you sign in as the seller and click "I agree"
  3. you paste back the address the browser lands on (or just the code= value)
  4. it trades the code for tokens (grant_type=authorization_code, the code lives ~5 min)
  5. it writes ONLY the refresh token to .env as EBAY_OAUTH_REFRESH_TOKEN (quoted — it
     holds '#'), backs the old .env up to .tmp/, clears the cached access tokens, and
     proves the new token works with a refresh grant per scope set.

Needs EBAY_APP_ID / EBAY_CERT_ID (the same client pair as ebay_analytics) and the app's
RuName (eBay redirect URI name) in EBAY_RUNAME, or --runame (then saved to .env too).

Usage:
  python tools/ebay_consent.py                      # interactive
  python tools/ebay_consent.py --code "<URL or code>"   # non-interactive step 3
  python tools/ebay_consent.py --dry-run            # print the consent URL only
"""

import argparse
import base64
import os
import shutil
import sys
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from tools import ebay_analytics  # noqa: E402 — token endpoint + HTTP seam

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
BACKUP_DIR = ROOT / ".tmp"
AUTHORIZE_URL = "https://auth.ebay.com/oauth2/authorize"
_SCOPE = "https://api.ebay.com/oauth/api_scope"
# Every scope the tool asks for, grouped as each caller mints them (a refresh grant may ask
# for any subset of what was consented, but one unconsented scope fails the whole request).
SCOPE_SETS = {
    "Trading API (ebay_sync)":      (_SCOPE,),
    "views + fees (ebay_analytics)": ebay_analytics.SCOPES,
    "photo hosting (upload_photos)": (f"{_SCOPE}/sell.inventory",),
}
CONSENT_SCOPES = tuple(dict.fromkeys(s for scopes in SCOPE_SETS.values() for s in scopes))
TOKEN_CACHES = ("data/.ebay_user_token.json", "data/.ebay_media_token.json",
                "data/.ebay_trading_token.json")


def consent_url(app_id: str, runame: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": app_id, "response_type": "code", "redirect_uri": runame,
        "scope": " ".join(CONSENT_SCOPES), "prompt": "login"})


def extract_code(pasted: str) -> str:
    """The authorization code from the landing URL (any eBay host — the RuName's accept URL
    or eBay's own ThirdPartyAuthSucessFailure page), or a bare / still-URL-encoded code."""
    text = (pasted or "").strip().strip('"').strip("'")
    if "code=" in text:
        query = urllib.parse.urlsplit(text).query or text.split("?", 1)[-1]
        values = urllib.parse.parse_qs(query).get("code")
        return values[0].strip() if values else ""
    if "://" in text or "?" in text:
        return ""                                   # a URL with no code (e.g. consent declined)
    return urllib.parse.unquote(text)


def exchange_code(code: str, app_id: str, cert_id: str, runame: str) -> dict:
    """authorization_code grant -> {ok, refresh_token, refresh_expires_in, message}. Never raises."""
    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    req = urllib.request.Request(
        ebay_analytics.TOKEN_URL, method="POST",
        data=urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": runame}).encode(),
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    status, data = ebay_analytics._send(req)
    refresh = data.get("refresh_token") if status == 200 else None
    if not refresh:
        return {"ok": False, "refresh_token": None, "refresh_expires_in": None,
                "message": ebay_analytics._error_text(status, data)}
    return {"ok": True, "refresh_token": refresh,
            "refresh_expires_in": data.get("refresh_token_expires_in"), "message": ""}


def set_env_value(path: Path, key: str, value: str) -> None:
    """Replace `key=...` in the .env (or append it), value double-quoted; every other line is
    kept byte-for-byte. Atomic write."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    new_line = f'{key}="{value}"\n'
    for i, line in enumerate(lines):
        if line.lstrip().startswith(f"{key}=") or line.lstrip().startswith(f"{key} ="):
            lines[i] = new_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, path)


def verify(refresh_token: str) -> dict:
    """{label: (ok, error)} — one refresh grant per scope set, nothing cached."""
    os.environ["EBAY_OAUTH_REFRESH_TOKEN"] = refresh_token
    out = {}
    BACKUP_DIR.mkdir(exist_ok=True)
    for n, (label, scopes) in enumerate(SCOPE_SETS.items()):
        probe = BACKUP_DIR / f".consent_probe_{n}.json"
        token = ebay_analytics.get_user_access_token(force=True, scopes=scopes, cache_path=probe)
        out[label] = (bool(token), ebay_analytics.last_token_error)
        probe.unlink(missing_ok=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="New eBay OAuth refresh token -> .env")
    ap.add_argument("--runame", help="eBay RuName (redirect URI name); saved to .env as EBAY_RUNAME")
    ap.add_argument("--code", help="the landing URL (or code) after signing in; skips the prompt")
    ap.add_argument("--dry-run", action="store_true", help="print the consent URL only, write nothing")
    ap.add_argument("--no-browser", action="store_true", help="don't open the consent URL")
    args = ap.parse_args(argv)

    load_dotenv(ENV_PATH)
    app_id = os.getenv("EBAY_APP_ID", "").strip()
    cert_id = os.getenv("EBAY_CERT_ID", "").strip()
    runame = (args.runame or os.getenv("EBAY_RUNAME", "")).strip()
    if not (app_id and cert_id):
        print("EBAY_APP_ID / EBAY_CERT_ID missing from .env")
        return 1
    if not runame:
        print("No RuName: pass --runame <name> (developer.ebay.com > User Tokens > your RuName)")
        return 1

    url = consent_url(app_id, runame)
    print("\n1. Sign in to eBay as the SELLER account and click 'I agree':\n")
    print(url + "\n")
    if args.dry_run:
        return 0
    if args.runame and args.runame.strip() != os.getenv("EBAY_RUNAME", "").strip():
        set_env_value(ENV_PATH, "EBAY_RUNAME", runame)
    if not args.code and not args.no_browser:
        webbrowser.open(url)

    pasted = args.code or input("2. Paste the FULL address the browser landed on, then Enter:\n> ")
    code = extract_code(pasted)
    if not code:
        print("No code= in that — paste the whole address bar after clicking 'I agree'.")
        return 1

    res = exchange_code(code, app_id, cert_id, runame)
    if not res["ok"]:
        print(f"Token swap failed: {res['message']}\n(The code lives ~5 minutes and works once — "
              "run the script again and paste the new address straight away.)")
        return 1

    BACKUP_DIR.mkdir(exist_ok=True)
    if ENV_PATH.exists():
        backup = BACKUP_DIR / f".env.backup-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(ENV_PATH, backup)
        print(f"Old .env backed up to {backup}")
    set_env_value(ENV_PATH, "EBAY_OAUTH_REFRESH_TOKEN", res["refresh_token"])
    for rel in TOKEN_CACHES:                       # minted from the OLD token — drop them
        (ROOT / rel).unlink(missing_ok=True)
    days = int(res["refresh_expires_in"] or 0) // 86400
    print(f"\nDONE: refresh token written to .env (valid ~{days} days).\n")

    failed = 0
    for label, (ok, err) in verify(res["refresh_token"]).items():
        print(f"  {'OK  ' if ok else 'FAIL'} {label}" + ("" if ok else f" — {err}"))
        failed += not ok
    if failed:
        print("\nA scope failed: it wasn't granted on the consent screen. Re-run and accept all.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
