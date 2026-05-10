"""
Tool: knowledge_store
Persistent learning store for community research signals.

Stores per-source rolling stats (hit_rate, freshness, intent_rate) and computes
adaptive weights so sources that consistently produce fresh, on-topic, purchase-
intent matches get queried more aggressively, while stale ones get demoted and
eventually retired.

Files in data/knowledge/:
  sources.json       — per-source stats + current weight + status
  keywords.json      — per-category keyword recurrence counts
  links.json         — high-yield URLs (deduped, with last_visited)
  products/{slug}.json — per-product research history

API surface:
  load_sources() / save_sources(d)
  ensure_seed_sources(categories_yaml_dict) — imports yaml registry on first run
  active_sources_for(category, source_type=None) — what to query right now
  update_after_run(scout_results, category, today=None) — applies rolling-avg
                                                          updates + promote/demote
  record_product_run(slug, payload) — append to products/{slug}.json
  bump_keyword(category, keyword, was_intent=False)
  bump_link(url, source_id, was_intent=False)
  domain_recurrence_promote(candidate_domains, category, source_type)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from urllib.parse import urlparse

from loguru import logger


# ── Paths ──────────────────────────────────────────────────────────────────────

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE_DIR = os.path.join(ROOT, "data", "knowledge")
PRODUCTS_DIR  = os.path.join(KNOWLEDGE_DIR, "products")
SOURCES_PATH  = os.path.join(KNOWLEDGE_DIR, "sources.json")
KEYWORDS_PATH = os.path.join(KNOWLEDGE_DIR, "keywords.json")
LINKS_PATH    = os.path.join(KNOWLEDGE_DIR, "links.json")


# ── Tunables ───────────────────────────────────────────────────────────────────

ROLLING_N = 10                # rolling-average window for hit/freshness/intent
DEMOTE_FRESHNESS_THRESH = 0.10
DEMOTE_CONSEC_MISSES    = 3
RETIRE_RUNS_SINCE_YIELD = 10
CANDIDATE_DOMAIN_THRESH = 3   # web_scout sees same domain N times → candidate
PROMOTE_AFTER_YIELDS    = 1   # candidate → active after N yields
PROMOTE_WITHIN_RUNS     = 3   # within this many runs

BASE_WEIGHT = {
    "reddit":  1.0,
    "youtube": 1.2,
    "forum":   1.5,
    "web":     0.6,
}

INITIAL_CANDIDATE_WEIGHT = 0.4


# ── IO helpers ─────────────────────────────────────────────────────────────────

def _ensure_dirs():
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    os.makedirs(PRODUCTS_DIR, exist_ok=True)


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.warning(f"  knowledge_store: {path} corrupted ({e}); starting fresh")
        return default


def _save_json(path, data):
    _ensure_dirs()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=str)


def load_sources():    return _load_json(SOURCES_PATH, {})
def save_sources(d):   _save_json(SOURCES_PATH, d)
def load_keywords():   return _load_json(KEYWORDS_PATH, {})
def save_keywords(d):  _save_json(KEYWORDS_PATH, d)
def load_links():      return _load_json(LINKS_PATH, {})
def save_links(d):     _save_json(LINKS_PATH, d)


# ── Source ID helpers ──────────────────────────────────────────────────────────

def make_source_id(source_type: str, identifier: str) -> str:
    """
    'reddit:r/Silverbugs', 'forum:kitcometals.com', 'youtube:channel', 'web:domain'.
    Identifier is normalized to lowercase except subreddit casing.
    """
    if source_type == "reddit":
        ident = identifier if identifier.startswith("r/") else f"r/{identifier}"
    else:
        ident = identifier.lower()
    return f"{source_type}:{ident}"


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


# ── Seed import ────────────────────────────────────────────────────────────────

def ensure_seed_sources(categories_cfg: dict):
    """
    On first run, import yaml registry into sources.json. Idempotent — only adds
    sources not already present. Existing entries (with their accumulated stats)
    are preserved.
    """
    sources = load_sources()
    today_s = date.today().isoformat()
    added = 0

    for cat_name, cfg in categories_cfg.items():
        cs = cfg.get("community_sources", {})
        # Tolerate both old-style flat list and new dict schema
        if isinstance(cs, list):
            for entry in cs:
                if isinstance(entry, dict) and "subreddit" in entry:
                    sid = make_source_id("reddit", entry["subreddit"])
                    if sid not in sources:
                        sources[sid] = _new_source_entry("reddit", [cat_name], today_s)
                        added += 1
            continue

        for sub in cs.get("reddit", []):
            sid = make_source_id("reddit", sub)
            if sid not in sources:
                sources[sid] = _new_source_entry("reddit", [cat_name], today_s)
                added += 1
            elif cat_name not in sources[sid]["categories"]:
                sources[sid]["categories"].append(cat_name)

        for forum in cs.get("forums", []):
            url  = forum.get("url", "") if isinstance(forum, dict) else forum
            host = domain_of(url) or url.lower()
            sid  = make_source_id("forum", host)
            if sid not in sources:
                e = _new_source_entry("forum", [cat_name], today_s)
                e["search_path"] = forum.get("search_path", "") if isinstance(forum, dict) else ""
                e["base_url"]    = url
                sources[sid] = e
                added += 1
            elif cat_name not in sources[sid]["categories"]:
                sources[sid]["categories"].append(cat_name)

    if added:
        save_sources(sources)
        logger.info(f"  knowledge_store: seeded {added} new source(s)")


def _new_source_entry(source_type: str, categories: list[str], today_s: str) -> dict:
    return {
        "categories":          list(categories),
        "type":                source_type,
        "hit_rate":            0.0,
        "freshness_avg":       0.0,
        "intent_rate":         0.0,
        "weight":              BASE_WEIGHT.get(source_type, 1.0),
        "runs":                0,
        "yields":              0,
        "consecutive_misses":  0,
        "runs_since_yield":    0,
        "last_run":            None,
        "status":              "active",
    }


# ── Active source query ────────────────────────────────────────────────────────

def active_sources_for(category: str, source_type: str | None = None) -> list[tuple[str, dict]]:
    """
    Return (source_id, source_dict) for every active or candidate source whose
    categories list includes `category`. Retired sources are skipped.
    """
    sources = load_sources()
    out = []
    for sid, s in sources.items():
        if s.get("status") == "retired":
            continue
        if source_type and s.get("type") != source_type:
            continue
        if category in s.get("categories", []):
            out.append((sid, s))
    return out


# ── Update after run ───────────────────────────────────────────────────────────

def update_after_run(scout_results: list, category: str, today: date | None = None):
    """
    Apply rolling-avg updates, recompute weights, and run promote/demote logic.
    Each scout_result is expected to expose: source_id, freshness, intent_rate,
    mention_count, candidate_domains (list[str], may be empty).
    """
    sources = load_sources()
    today_s = (today or date.today()).isoformat()

    for r in scout_results:
        sid = r.source_id
        prev = sources.get(sid) or _new_source_entry(r.source_type, [category], today_s)
        if category not in prev["categories"]:
            prev["categories"].append(category)

        hit_now = 1.0 if r.mention_count > 0 else 0.0
        prev["hit_rate"]      = _rolling_avg(prev["hit_rate"],      hit_now,           ROLLING_N)
        prev["freshness_avg"] = _rolling_avg(prev["freshness_avg"], r.freshness,        ROLLING_N)
        prev["intent_rate"]   = _rolling_avg(prev["intent_rate"],   r.intent_rate,      ROLLING_N)
        prev["runs"]          = prev.get("runs", 0) + 1
        prev["last_run"]      = today_s

        if r.mention_count > 0:
            prev["yields"]              = prev.get("yields", 0) + 1
            prev["runs_since_yield"]    = 0
            prev["consecutive_misses"]  = 0
        else:
            prev["consecutive_misses"]  = prev.get("consecutive_misses", 0) + 1
            prev["runs_since_yield"]    = prev.get("runs_since_yield", 0) + 1

        prev["weight"] = _compute_weight(prev)
        _apply_status_transitions(prev)
        sources[sid] = prev

    # Candidate-domain promotion via web_scout
    for r in scout_results:
        if r.source_type != "web":
            continue
        for dom in (r.candidate_domains or []):
            _bump_candidate(sources, dom, category, today_s)

    save_sources(sources)


def _rolling_avg(prev: float, current: float, n: int) -> float:
    """Exponential-style rolling average over N runs (no full history needed)."""
    if prev == 0.0:
        return float(current)
    return ((n - 1) * prev + current) / n


def _compute_weight(s: dict) -> float:
    base = BASE_WEIGHT.get(s["type"], 1.0)
    if s.get("status") == "candidate":
        base = INITIAL_CANDIDATE_WEIGHT
    w = (
        base
        * (0.3 + 0.7 * s["hit_rate"])
        * (0.3 + 0.7 * s["freshness_avg"])
        * (0.5 + 0.5 * s["intent_rate"])
    )
    return round(max(w, 0.05), 3)


def _apply_status_transitions(s: dict):
    status = s.get("status", "active")

    if status == "candidate":
        if s["yields"] >= PROMOTE_AFTER_YIELDS and s["runs"] <= PROMOTE_WITHIN_RUNS + 1:
            s["status"] = "active"
            s.pop("demoted_reason", None)
        elif s["runs"] >= PROMOTE_WITHIN_RUNS and s["yields"] == 0:
            s["status"] = "retired"
            s["demoted_reason"] = "candidate failed to yield within window"
        return

    if status == "active":
        if (s["freshness_avg"] < DEMOTE_FRESHNESS_THRESH and
                s["consecutive_misses"] >= DEMOTE_CONSEC_MISSES):
            s["status"] = "demoted"
            s["demoted_reason"] = (
                f"freshness<{DEMOTE_FRESHNESS_THRESH} for "
                f"{s['consecutive_misses']} consecutive runs"
            )
            s["weight"] = max(s["weight"] * 0.2, 0.05)
        return

    if status == "demoted":
        if s["runs_since_yield"] >= RETIRE_RUNS_SINCE_YIELD:
            s["status"] = "retired"
            s["demoted_reason"] = (s.get("demoted_reason", "") +
                                   f"; retired after {RETIRE_RUNS_SINCE_YIELD} dry runs")
        elif s["consecutive_misses"] == 0:
            s["status"] = "active"
            s.pop("demoted_reason", None)


def _bump_candidate(sources: dict, domain: str, category: str, today_s: str):
    """Track recurrence of a candidate domain; promote to candidate after N hits."""
    if not domain:
        return
    sid = make_source_id("forum", domain)   # candidate forums via web_scout
    if sid in sources:
        return
    bucket_key = f"_recurrence:{sid}"
    if bucket_key not in sources:
        sources[bucket_key] = {"hits": 0, "last_seen": today_s, "category": category}
    sources[bucket_key]["hits"] += 1
    sources[bucket_key]["last_seen"] = today_s

    if sources[bucket_key]["hits"] >= CANDIDATE_DOMAIN_THRESH:
        e = _new_source_entry("forum", [category], today_s)
        e["status"] = "candidate"
        e["base_url"] = f"https://{domain}"
        e["weight"] = INITIAL_CANDIDATE_WEIGHT
        sources[sid] = e
        del sources[bucket_key]
        logger.info(f"  knowledge_store: promoted {domain} to candidate forum source")


# ── Keywords + links ───────────────────────────────────────────────────────────

def bump_keyword(category: str, keyword: str, was_intent: bool = False):
    keyword = keyword.strip().lower()
    if not keyword or len(keyword) < 3:
        return
    kw = load_keywords()
    cat = kw.setdefault(category, {})
    entry = cat.setdefault(keyword, {"mentions": 0, "intent_hits": 0, "last_seen": ""})
    entry["mentions"]   = entry.get("mentions", 0) + 1
    if was_intent:
        entry["intent_hits"] = entry.get("intent_hits", 0) + 1
    entry["last_seen"] = date.today().isoformat()
    save_keywords(kw)


def bump_link(url: str, source_id: str, was_intent: bool = False):
    if not url or not url.startswith("http"):
        return
    links = load_links()
    entry = links.setdefault(url, {"source_id": source_id, "hits": 0, "intent_hits": 0,
                                    "first_seen": date.today().isoformat()})
    entry["hits"] = entry.get("hits", 0) + 1
    if was_intent:
        entry["intent_hits"] = entry.get("intent_hits", 0) + 1
    entry["last_visited"] = date.today().isoformat()
    save_links(links)


# ── Per-product history ────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")

def slugify(title: str) -> str:
    s = _SLUG_RE.sub("-", title.lower()).strip("-")
    return s[:80] or "untitled"


def record_product_run(slug: str, payload: dict):
    """
    Append a research run to data/knowledge/products/{slug}.json. Creates the
    file if missing. Caller passes the full payload (date, signal_strength,
    by_source dict, top_links, intent_phrases_seen, brand, model, etc.).
    """
    _ensure_dirs()
    path = os.path.join(PRODUCTS_DIR, f"{slug}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    else:
        doc = {
            "slug": slug,
            "title": payload.get("title", ""),
            "category": payload.get("category", ""),
            "costco_url": payload.get("costco_url", ""),
            "brand": payload.get("brand"),
            "model": payload.get("model"),
            "first_researched": payload.get("date", date.today().isoformat()),
            "research_runs": [],
        }

    # Keep top-level brand/model fresh if newly extracted
    if payload.get("brand") and not doc.get("brand"):
        doc["brand"] = payload["brand"]
    if payload.get("model") and not doc.get("model"):
        doc["model"] = payload["model"]

    run_entry = {k: v for k, v in payload.items()
                 if k not in {"title", "category", "costco_url"}}
    run_entry.setdefault("date", date.today().isoformat())
    doc["research_runs"].append(run_entry)
    # Cap history length to avoid unbounded files
    if len(doc["research_runs"]) > 50:
        doc["research_runs"] = doc["research_runs"][-50:]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, default=str)


def load_product_history(slug: str) -> dict | None:
    path = os.path.join(PRODUCTS_DIR, f"{slug}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
