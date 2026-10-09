"""
Tool: discovery_cadence
Catalog-exhaustion signal — how many NEW unique Costco products discovery surfaces per
day, per category, so we can see when a category's discovery stream is fully mapped.

Pure side-effect tracker: called once at the end of costco_discovery.discover_all(); it
never changes what discovery returns or which PENDING rows get added, and it never raises.

  data/discovery_seen.json     {"<costco product id>": "2026-10-09"}  first-seen date (UTC)
  data/discovery_cadence.json  [{"date", "total_seen", "new_today", "per_category":
                                   {cat: {"total", "new", "seen_total", "capped"}}}]  last 90 days

CAVEAT: discovery is capped at max_discovery (default 60) per category and only reads the
first API page of each discovery URL. "EXHAUSTED" therefore means "the discovery stream at
current settings is returning nothing new", NOT "Costco has no more products in this
category". `capped` marks categories that hit the cap — for those, raising the cap or
paginating is a separate future decision.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from loguru import logger

from tools.sale_schedule import costco_product_id

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEN_PATH = os.path.join(_BASE_DIR, "data", "discovery_seen.json")
CADENCE_PATH = os.path.join(_BASE_DIR, "data", "discovery_cadence.json")

KEEP_ENTRIES = 90          # daily rows kept in the cadence log
EXHAUST_WINDOW_DAYS = 7    # "no new uniques in the last N days"
EXHAUST_MIN_RUNS = 3       # ...and the category was discovered on at least N days

CAVEAT = ("EXHAUSTED = the discovery stream at current settings (max_discovery cap, first "
          "API page per URL) returns nothing new — NOT that Costco has no more products in "
          "the category. (capped) = the category hit its max_discovery cap on the last run.")


def product_key(url):
    """Costco product id (both URL shapes map to the same id), else the stripped URL."""
    pid = costco_product_id(url)
    if pid:
        return pid
    return re.split(r"[?#]", (url or "").strip(), maxsplit=1)[0]


def _today_utc():
    return datetime.now(timezone.utc).date().isoformat()


def _load(path, default):
    """JSON from path; `default` when missing, corrupt or the wrong type. Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        logger.warning(f"discovery_cadence: unreadable {os.path.basename(path)} ({e}) — starting fresh")
        return default
    if not isinstance(data, type(default)):
        logger.warning(f"discovery_cadence: {os.path.basename(path)} has the wrong shape — starting fresh")
        return default
    return data


def _save(path, data):
    """Atomic write; logs and carries on when the disk refuses."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"discovery_cadence: could not write {os.path.basename(path)}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def load_cadence():
    return [e for e in _load(CADENCE_PATH, []) if isinstance(e, dict) and e.get("date")]


def _prev_seen_total(cadence, category):
    for entry in reversed(cadence):
        cat = (entry.get("per_category") or {}).get(category)
        if isinstance(cat, dict):
            return int(cat.get("seen_total", 0) or 0)
    return 0


def track_discovery(products, capped=None, today=None):
    """
    Record one discovery run. Returns the day's cadence entry (`last_run_new` = new this
    call), or None on any failure — a tracker error must never fail discovery.
    """
    try:
        today = today or _today_utc()
        capped = set(capped or ())
        seen = _load(SEEN_PATH, {})
        cadence = load_cadence()

        run = {}            # category -> {"total", "new"}
        counted = set()     # keys already tallied this run (two URL shapes, one product)
        new_run = 0
        for p in products or []:
            key = product_key(p.get("url"))
            if not key or key in counted:
                continue
            counted.add(key)
            cat = run.setdefault(p.get("category") or "Unknown", {"total": 0, "new": 0})
            cat["total"] += 1
            if key not in seen:
                seen[key] = today
                cat["new"] += 1
                new_run += 1

        # One row per day: a second run the same day folds into it.
        if cadence and cadence[-1].get("date") == today:
            entry = cadence[-1]
        else:
            entry = {"date": today, "total_seen": 0, "new_today": 0, "per_category": {}}
            cadence.append(entry)
        per_cat = entry.setdefault("per_category", {})
        for name, counts in run.items():
            prev = per_cat.get(name) or {}
            per_cat[name] = {
                "total": counts["total"],
                "new": int(prev.get("new", 0) or 0) + counts["new"],
                "seen_total": _prev_seen_total(cadence, name) + counts["new"],
                "capped": name in capped,
            }
        entry["total_seen"] = len(seen)
        entry["new_today"] = int(entry.get("new_today", 0) or 0) + new_run
        entry["last_run_new"] = new_run     # the scheduler reads this back after its subprocess

        _save(SEEN_PATH, seen)
        _save(CADENCE_PATH, cadence[-KEEP_ENTRIES:])
        return dict(entry)
    except Exception as e:
        logger.warning(f"discovery_cadence: tracking failed (discovery unaffected): {e}")
        return None


def category_stats(cadence, today=None):
    """
    {category: {"seen_total", "new_7d", "runs", "capped", "exhausted"}}.
    A category's first appearance in the log is its initial mapping (everything is "new"
    then), so it doesn't count toward new_7d.
    """
    today = date.fromisoformat(today or _today_utc())
    window_start = (today - timedelta(days=EXHAUST_WINDOW_DAYS - 1)).isoformat()
    stats = {}
    for entry in sorted(cadence, key=lambda e: e.get("date", "")):
        for name, cat in (entry.get("per_category") or {}).items():
            if not isinstance(cat, dict):
                continue
            s = stats.get(name)
            if s is None:
                s = stats[name] = {"seen_total": 0, "new_7d": 0, "runs": 0, "capped": False}
            elif entry["date"] >= window_start:
                s["new_7d"] += int(cat.get("new", 0) or 0)
            s["runs"] += 1
            s["seen_total"] = int(cat.get("seen_total", 0) or 0)
            s["capped"] = bool(cat.get("capped"))
    for s in stats.values():
        s["exhausted"] = s["new_7d"] == 0 and s["runs"] >= EXHAUST_MIN_RUNS
    return stats


def format_stats_table(stats):
    if not stats:
        return ("No discovery runs tracked yet — the table fills after the next discovery run.\n"
                + CAVEAT)
    width = max(len("Category"), *(len(n) for n in stats))
    lines = [f"{'Category':<{width}}  {'Seen':>6}  {'New 7d':>6}  {'Runs':>4}  EXHAUSTED?",
             "-" * (width + 36)]
    for name in sorted(stats):
        s = stats[name]
        flag = "YES" if s["exhausted"] else "no"
        if s["capped"]:
            flag += " (capped)"
        lines.append(f"{name:<{width}}  {s['seen_total']:>6}  {s['new_7d']:>6}  {s['runs']:>4}  {flag}")
    lines += ["", CAVEAT]
    return "\n".join(lines)
