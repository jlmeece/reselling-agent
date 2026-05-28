"""
Tool: cookie_sync
Syncs data/costco_cookies.json between local Windows machine and VPS
via a private GitHub Gist. No new dependencies — uses stdlib urllib only.

LOCAL (Windows) — after refreshing cookies:
    python tools/cookie_sync.py upload

VPS (Ubuntu) — before starting Chrome-dependent modes:
    python tools/cookie_sync.py download

First-time setup:
    1. Create a GitHub Personal Access Token at github.com/settings/tokens
       with only the 'gist' scope checked.
    2. Add GITHUB_TOKEN=<token> to .env.
    3. Run `python tools/cookie_sync.py upload` — it will print the Gist ID.
    4. Add GIST_ID=<printed id> to .env on both local and VPS.
    5. Future uploads/downloads use that Gist ID automatically.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime
from dotenv import load_dotenv
from loguru import logger

load_dotenv(encoding="utf-8", override=True)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIES_PATH  = os.path.join(_PROJECT_ROOT, "data", "costco_cookies.json")
GIST_FILENAME = "costco_cookies.json"
GIST_API_BASE = "https://api.github.com/gists"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(url: str, method: str, token: str, payload: dict = None) -> dict:
    body = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=body, method=method)
    for k, v in _headers(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        logger.error(f"GitHub API {e.code} — {err_body}")
        sys.exit(1)
    except urllib.error.URLError as e:
        logger.error(f"Network error: {e.reason}")
        sys.exit(1)


def upload_cookies() -> str:
    """
    Reads data/costco_cookies.json and pushes it to a private GitHub Gist.

    - GIST_ID set in .env  → patches the existing Gist (idempotent).
    - GIST_ID not set      → creates a new private Gist, prints the ID.

    Returns the Gist ID (existing or newly created).
    """
    token   = os.getenv("GITHUB_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()

    if not token:
        logger.error("GITHUB_TOKEN not set in .env. Cannot upload.")
        sys.exit(1)

    if not os.path.exists(COOKIES_PATH):
        logger.error(f"No cookies file at {COOKIES_PATH}. Run .\\run.ps1 cookies first.")
        sys.exit(1)

    with open(COOKIES_PATH, encoding="utf-8") as f:
        content = f.read()

    # Validate it's real JSON before uploading
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"Cookies file is not valid JSON: {e}")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {
        "description": f"WAT Reselling Agent — Costco cookies synced {timestamp}",
        "public": False,
        "files": {GIST_FILENAME: {"content": content}},
    }

    if gist_id:
        logger.info(f"Updating Gist {gist_id}...")
        result = _request(f"{GIST_API_BASE}/{gist_id}", "PATCH", token, payload)
        logger.info(f"Upload complete. {len(parsed)} cookies synced to Gist {gist_id}.")
    else:
        logger.info("GIST_ID not set — creating new private Gist...")
        result = _request(GIST_API_BASE, "POST", token, payload)
        gist_id = result.get("id", "")
        logger.info(
            f"Gist created with {len(parsed)} cookies.\n"
            f"\n  Add to .env on both local and VPS:\n"
            f"  GIST_ID={gist_id}\n"
        )

    return gist_id


def download_cookies() -> str:
    """
    Downloads cookies from the GitHub Gist and writes them to
    data/costco_cookies.json. Run this on the VPS before any Chrome-dependent mode.

    Returns the local path written.
    """
    token   = os.getenv("GITHUB_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()

    if not token:
        logger.error("GITHUB_TOKEN not set in .env. Cannot download.")
        sys.exit(1)
    if not gist_id:
        logger.error(
            "GIST_ID not set in .env. "
            "Run 'python tools/cookie_sync.py upload' on your local machine first."
        )
        sys.exit(1)

    logger.info(f"Fetching Gist {gist_id}...")
    result = _request(f"{GIST_API_BASE}/{gist_id}", "GET", token)

    files    = result.get("files", {})
    file_obj = files.get(GIST_FILENAME)
    if not file_obj:
        logger.error(f"File '{GIST_FILENAME}' not found in Gist {gist_id}.")
        sys.exit(1)

    content = file_obj.get("content")

    # GitHub truncates Gist files larger than 1MB — fetch via raw_url in that case
    if not content or file_obj.get("truncated"):
        raw_url = file_obj.get("raw_url")
        if not raw_url:
            logger.error("Gist content is truncated and no raw_url is available.")
            sys.exit(1)
        logger.info("Gist content truncated — fetching via raw_url...")
        req = urllib.request.Request(raw_url)
        for k, v in _headers(token).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode()

    # Validate before writing — don't overwrite a good cookies file with garbage
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"Downloaded content is not valid JSON: {e}")
        sys.exit(1)

    os.makedirs(os.path.dirname(COOKIES_PATH), exist_ok=True)
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    updated_at = result.get("updated_at", "unknown")
    logger.info(
        f"Download complete. {len(parsed)} cookies written to {COOKIES_PATH} "
        f"(Gist last updated: {updated_at})"
    )
    return COOKIES_PATH


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sync Costco cookies via GitHub Gist")
    parser.add_argument(
        "action", choices=["upload", "download"],
        help="upload: push local cookies to Gist | download: pull Gist cookies to local",
    )
    args = parser.parse_args()

    if args.action == "upload":
        upload_cookies()
    else:
        download_cookies()
