"""
Refuse to start a Chrome probe while a scheduler run holds data/.scheduler_lock.

Probes take no lock, and make_browser can kill the shared agent Chrome on a warm-up timeout. On
2026-11-07 a savings probe during a manual research run coincided with 6 rows written CHECK FAILED
with a fallback eBay price. `--force` skips the check.
"""
import os
import sys
import time

LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".scheduler_lock")
LOCK_STALE_MINUTES = 45      # same as agents/scheduler.py: an older lock is a crashed run, not a live one


def lock_holder(path=LOCK_FILE, stale_minutes=LOCK_STALE_MINUTES, now=None):
    """'<mode> <started>' of a fresh scheduler lock, or None (no lock, or stale)."""
    try:
        age_min = ((now or time.time()) - os.path.getmtime(path)) / 60
    except OSError:
        return None
    if age_min >= stale_minutes:
        return None
    try:
        with open(path) as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"


def refuse_if_scheduler_busy(argv=None, path=LOCK_FILE):
    """Exit 2 with a message when a scheduler run holds the lock, unless --force is in argv."""
    argv = sys.argv if argv is None else argv
    if "--force" in argv:
        return
    held = lock_holder(path)
    if held:
        sys.exit(f"A scheduler run holds {path} ({held}). A probe now can kill its Chrome — "
                 f"wait for it to finish, or pass --force.")
