"""
tools.scouts.web_scout
General web search scout. Two backends, picked in this order:

  1. Google Custom Search JSON API — cleanest data, 100 queries/day free.
     Requires GOOGLE_CSE_KEY + GOOGLE_CSE_ID in .env.
  2. DuckDuckGo HTML fallback — no API key. Less reliable; rate-limited; uses
     the existing CDP Chrome session for stealth.

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
DDG_HTML_URL   = "https://html.duckduckgo.com/html/?q={query}"

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
        logger.debug(f"  web_scout CSE failed: {e}")
        return []
    return data.get("items", []) or []


def _via_duckduckgo(query: str) -> list[dict]:
    """
    Fallback using DuckDuckGo's HTML endpoint via urllib (no Playwright).
    Playwright sync API can't start a second session while the main CDP Chrome
    session is already running in the researcher loop.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        url = DDG_HTML_URL.format(query=urllib.parse.quote(query))
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug(f"  web_scout DDG fetch failed: {e}")
        return []

    items: list[dict] = []
    # Parse result links from DDG HTML — format: <a class="result__a" href="...">title</a>
    matches = list(re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL
    ))[:MAX_RESULTS_PER_TERM]
    for m in matches:
        href  = m.group(1).strip()
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if href and title:
            items.append({"link": href, "title": title, "snippet": ""})

    return items


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
        items = _via_google_cse(query) or _via_duckduckgo(query)
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

    note = f"queries={rendered!r}; backends="
    note += "google_cse" if os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_ID") else "duckduckgo"

    return [ScoutResult.from_posts(
        source_id="web:search",
        source_type="web",
        posts=posts,
        candidate_domains=candidate_domains,
        note=note,
    )]
