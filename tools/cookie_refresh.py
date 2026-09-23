"""
Tool: cookie_refresh
Shared Costco cookie auto-refresh mechanics: runs setup_costco_session.py to
export fresh cookies from the agent's already-logged-in Chrome (via CDP),
then uploads them to the VPS via cookie_sync.py.

Imports nothing from agents/ — callable from both agents/scheduler.py
(age-based trigger) and tools/costco_scraper.py (expiry-based trigger)
without a circular import. Owns the shared 24h throttle so both trigger
paths draw from the same refresh-attempt budget. Sends no Telegram
messages itself — callers own their own alerting.
"""

import json
import os
import subprocess
import sys
import time

from loguru import logger

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COOKIES_PATH = os.path.join(_REPO_ROOT, "data", "costco_cookies.json")
_COOKIE_AUTOREFRESH_TS_PATH = os.path.join(_REPO_ROOT, "data", ".cookie_autorefresh_ts")
_COOKIE_AUTOREFRESH_INTERVAL_SEC = 86400  # at most one auto-refresh attempt per 24h, shared
                                            # across the age-based and expiry-based triggers
_COOKIE_AUTOREFRESH_TIMEOUT_SEC = 60


def cookie_expiry_stats(path: str = _COOKIES_PATH) -> "tuple[int, int]":
    """
    Returns (expired, total) for the cookie export at `path`, counted the same
    way tools/costco_scraper._load_cookies does: cookies missing a name/value
    are skipped; a cookie is expired when its expirationDate/expires is > 0
    and in the past (session cookies with no expiry are never expired).
    Returns (0, 0) if the file is missing or unreadable. Never raises.
    """
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:
        return 0, 0
    now = time.time()
    expired = total = 0
    for c in raw:
        if not c.get("name") or not c.get("value"):
            continue
        total += 1
        exp = c.get("expirationDate") or c.get("expires")
        try:
            if exp and float(exp) > 0 and float(exp) < now:
                expired += 1
        except (TypeError, ValueError):
            pass
    return expired, total


def expiry_refresh_needed(expired: int, total: int) -> bool:
    """Same rule as costco_scraper._load_cookies: >20% of cookies expired, or more than 10."""
    return total > 0 and (expired > total * 0.2 or expired > 10)


def _tail_output(result, n_lines: int = 3) -> str:
    """Last few lines of a subprocess result's stdout+stderr, for alert diagnostics."""
    combined = "\n".join(x for x in (result.stdout, result.stderr) if x)
    lines = [l for l in combined.strip().splitlines() if l.strip()]
    return "\n".join(lines[-n_lines:])


def refresh_costco_cookies() -> "tuple[bool, str]":
    """
    Tries to refresh Costco cookies unattended: runs setup_costco_session.py
    (exports cookies from the agent's already-running, already-logged-in
    Chrome via CDP — it does NOT log in itself) and, if that produces fresh
    cookies, uploads them to the VPS via cookie_sync.py upload.

    Throttled to at most one attempt per _COOKIE_AUTOREFRESH_INTERVAL_SEC (24h)
    so a closed/unreachable Chrome doesn't retry on every scheduled run or
    every scrape that hits an expired-cookie check.

    Returns (success, diagnostic) — diagnostic is a short error excerpt for
    the caller's own fallback alert, empty on success or when skipped by
    throttle. Never raises.
    """
    last_attempt_ts = 0.0
    if os.path.exists(_COOKIE_AUTOREFRESH_TS_PATH):
        try:
            with open(_COOKIE_AUTOREFRESH_TS_PATH) as f:
                last_attempt_ts = float(f.read().strip())
        except Exception:
            last_attempt_ts = 0.0
    if time.time() - last_attempt_ts < _COOKIE_AUTOREFRESH_INTERVAL_SEC:
        logger.info("Cookie auto-refresh already attempted within last 24h, skipping.")
        return False, ""

    try:
        os.makedirs(os.path.dirname(_COOKIE_AUTOREFRESH_TS_PATH), exist_ok=True)
        with open(_COOKIE_AUTOREFRESH_TS_PATH, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        logger.warning(f"Cookie auto-refresh timestamp write failed (non-fatal): {e}")

    mtime_before = os.path.getmtime(_COOKIES_PATH) if os.path.exists(_COOKIES_PATH) else 0.0

    logger.info("Attempting automatic Costco cookie refresh...")
    try:
        export_result = subprocess.run(
            [sys.executable, os.path.join(_REPO_ROOT, "tools", "setup_costco_session.py")],
            capture_output=True, text=True, timeout=_COOKIE_AUTOREFRESH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Cookie auto-refresh: setup_costco_session.py timed out.")
        return False, "setup_costco_session.py timed out — is Chrome open and reachable?"
    except Exception as e:
        logger.warning(f"Cookie auto-refresh: setup_costco_session.py failed to run: {e}")
        return False, f"setup_costco_session.py failed to run: {e}"

    mtime_after = os.path.getmtime(_COOKIES_PATH) if os.path.exists(_COOKIES_PATH) else 0.0
    if export_result.returncode != 0 or mtime_after <= mtime_before:
        logger.warning("Cookie auto-refresh: setup_costco_session.py did not produce fresh cookies.")
        return False, _tail_output(export_result)

    try:
        upload_result = subprocess.run(
            [sys.executable, os.path.join(_REPO_ROOT, "tools", "cookie_sync.py"), "upload"],
            capture_output=True, text=True, timeout=_COOKIE_AUTOREFRESH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Cookie auto-refresh: cookie_sync.py upload timed out.")
        return False, "cookie_sync.py upload timed out."
    except Exception as e:
        logger.warning(f"Cookie auto-refresh: cookie_sync.py upload failed to run: {e}")
        return False, f"cookie_sync.py upload failed to run: {e}"

    if upload_result.returncode != 0:
        logger.warning("Cookie auto-refresh: cookie_sync.py upload failed.")
        return False, _tail_output(upload_result)

    logger.info("Cookie auto-refresh succeeded — cookies exported and synced to VPS.")
    return True, ""
