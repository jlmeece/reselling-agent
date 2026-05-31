"""
tools.scouts.web_scout
General web search scout. Three backends, tried in this order:

  1. Serper — Google results via API. Requires SERPER_API_KEY in .env.
  2. Brave Search API — Requires BRAVE_API_KEY in .env.
  3. Google Custom Search JSON API — last resort, 100 queries/day free.
     Requires GOOGLE_CSE_KEY + GOOGLE_CSE_ID in .env.

Beyond returning post-shaped results, web_scout surfaces `candidate_domains`
back to the orchestrator. The knowledge_store tracks recurrence: any domain
that shows up across 3+ products gets auto-promoted to a candidate forum
source for future runs.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime
from urllib.parse import urlparse

from loguru import logger

from .base_scout import Post, QueryContext, ScoutResult


GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"
SERPER_URL     = "https://google.serper.dev/search"
BRAVE_URL      = "https://api.search.brave.com/res/v1/web/search"

EXCLUDED_DOMAINS = {
    "pinterest.com", "ebay.com", "amazon.com", "walmart.com",
    "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
    "costco.com",   # we already came from there
}

MAX_TERMS_PER_RUN = 3
MAX_RESULTS_PER_TERM = 8


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _is_excluded(url: str) -> bool:
    d = _domain(url)
    return any(d == ex or d.endswith("." + ex) for ex in EXCLUDED_DOMAINS)


def _via_serper(query: str) -> list[dict]:
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return []
    try:
        import json as _json
        data = _json.dumps({"q": query, "num": MAX_RESULTS_PER_TERM}).encode()
        req = urllib.request.Request(
            SERPER_URL,
            data=data,
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = _json.loads(resp.read().decode())
        return [
            {"link": r.get("link",""), "title": r.get("title",""), "snippet": r.get("snippet","")}
            for r in results.get("organic", [])
            if not _is_excluded(r.get("link",""))
        ]
    except Exception as e:
        logger.debug(f"  Serper failed: {e}")
        return []


def _via_brave(query: str) -> list[dict]:
    key = os.getenv("BRAVE_API_KEY")
    if not key:
        return []
    try:
        params = urllib.parse.urlencode({"q": query, "count": MAX_RESULTS_PER_TERM})
        req = urllib.request.Request(
            f"{BRAVE_URL}?{params}",
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as _json
            results = _json.loads(resp.read().decode())
        return [
            {"link": r.get("url",""), "title": r.get("title",""), "snippet": r.get("description","")}
            for r in results.get("web", {}).get("results", [])
            if not _is_excluded(r.get("url",""))
        ]
    except Exception as e:
        logger.debug(f"  Brave search failed: {e}")
        return []


def _via_google_cse(query: str) -> list[dict]:
    key = os.getenv("GOOGLE_CSE_KEY")
    cx  = os.getenv("GOOGLE_CSE_ID")
    if not key or not cx:
        return []
    params = {"key": key, "cx": cx, "q": query, "num": MAX_RESULTS_PER_TERM}
    url = f"{GOOGLE_CSE_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        err = str(e)
        if "403" in err or "quota" in err.lower():
            logger.warning("  web_scout: Google CSE quota exhausted (100/day)")
        else:
            logger.debug(f"  web_scout CSE failed: {e}")
        return []
    return data.get("items", []) or []


def _parse_date_hint(snippet: str) -> date | None:
    """Pull a date hint from a snippet ('Mar 14, 2026 — ...')."""
    if not snippet:
        return None
    m = re.match(r"\s*([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4})", snippet)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).replace(",", ""), "%b %d %Y").date()
    except ValueError:
        try:
            return datetime.strptime(m.group(1).replace(",", ""), "%B %d %Y").date()
        except ValueError:
            return None


def run(query_ctx: QueryContext, category_config: dict) -> list[ScoutResult]:
    cs = category_config.get("community_sources", {})
    if not isinstance(cs, dict):
        return []
    terms = cs.get("web_terms", []) or []
    if not terms:
        return []

    rendered = []
    for t in terms[:MAX_TERMS_PER_RUN]:
        q = query_ctx.render_template(t)
        if q and q not in rendered:
            rendered.append(q)
    if not rendered:
        return []

    posts: list[Post] = []
    candidate_domains: list[str] = []
    seen_urls: set[str] = set()

    for query in rendered:
        items = _via_serper(query)
        if not items:
            items = _via_brave(query)
        if not items:
            items = _via_google_cse(query)
        for it in items:
            url = it.get("link") or ""
            if not url or url in seen_urls or _is_excluded(url):
                continue
            seen_urls.add(url)

            title   = it.get("title", "") or ""
            snippet = it.get("snippet", "") or ""
            posts.append(Post.make(
                title=title,
                url=url,
                date_=_parse_date_hint(snippet),
                snippet=snippet,
                engagement=0,
                source_id="web:search",
            ))
            d = _domain(url)
            if d and d not in candidate_domains:
                candidate_domains.append(d)

    active = (
        "serper" if os.getenv("SERPER_API_KEY") else
        "brave" if os.getenv("BRAVE_API_KEY") else
        "google_cse" if (os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_ID")) else
        "none"
    )
    note = f"queries={rendered!r}; backends=serper+brave+google_cse; primary={active}"

    return [ScoutResult.from_posts(
        source_id="web:search",
        source_type="web",
        posts=posts,
        candidate_domains=candidate_domains,
        note=note,
    )]
