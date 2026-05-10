"""
tools.scouts.reddit_scout
Reddit scout — searches each registered subreddit for the product query.

Refactored from the old `tools.community_signals` Reddit logic. Uses Reddit's
public JSON endpoint (no auth required) and is rate-limit-polite.

Each subreddit produces one ScoutResult so the knowledge_store can track per-
subreddit stats independently.
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from loguru import logger

from .base_scout import Post, QueryContext, ScoutResult


SUB_SEARCH_URL = (
    "https://www.reddit.com/r/{sub}/search.json"
    "?q={query}&sort=new&limit=15&restrict_sr=1&t=month"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; reselling-agent/1.0)",
}

REQUEST_TIMEOUT = 10
SLEEP_BETWEEN_QUERIES = (0.8, 1.5)


def _fetch(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"  reddit fetch failed: {e}")
        return {}


def _to_date(utc_ts) -> date | None:
    if not utc_ts:
        return None
    try:
        return datetime.fromtimestamp(float(utc_ts), tz=timezone.utc).date()
    except (TypeError, ValueError):
        return None


def _search_subreddit(subreddit: str, query: str) -> list[Post]:
    sub_clean = subreddit.lstrip("r/").lstrip("/").strip()
    url = SUB_SEARCH_URL.format(
        sub=urllib.parse.quote(sub_clean),
        query=urllib.parse.quote(query),
    )
    data = _fetch(url)
    children = data.get("data", {}).get("children", [])

    posts: list[Post] = []
    source_id = f"reddit:r/{sub_clean}"
    for item in children:
        d = item.get("data", {}) or {}
        title = d.get("title")
        if not title:
            continue
        posts.append(Post.make(
            title=title,
            url=f"https://reddit.com{d.get('permalink', '')}",
            date_=_to_date(d.get("created_utc")),
            snippet=(d.get("selftext") or "")[:400],
            engagement=int(d.get("score", 0) or 0) + int(d.get("num_comments", 0) or 0),
            source_id=source_id,
        ))
    return posts


def run(query_ctx: QueryContext, category_config: dict) -> list[ScoutResult]:
    """
    Returns one ScoutResult per configured subreddit. Empty list if no reddit
    sources are registered for this category.
    """
    cs = category_config.get("community_sources", {})
    # Backwards compat: tolerate old flat-list schema where each entry is a dict
    # with {subreddit, keywords}.
    subreddits: list[str] = []
    if isinstance(cs, dict):
        subreddits = list(cs.get("reddit", []))
    elif isinstance(cs, list):
        for entry in cs:
            if isinstance(entry, dict) and entry.get("subreddit"):
                subreddits.append(entry["subreddit"])

    if not subreddits:
        return []

    primary_query = query_ctx.best_query()
    out: list[ScoutResult] = []

    for sub in subreddits:
        try:
            posts = _search_subreddit(sub, primary_query)
        except Exception as e:
            logger.warning(f"  reddit_scout r/{sub} crashed: {e}")
            posts = []

        # If primary query was brand+model and got nothing, retry with bare title.
        if not posts and primary_query != query_ctx.title:
            try:
                posts = _search_subreddit(sub, query_ctx.title)
            except Exception:
                pass

        sub_clean = sub.lstrip("r/").lstrip("/").strip()
        out.append(ScoutResult.from_posts(
            source_id=f"reddit:r/{sub_clean}",
            source_type="reddit",
            posts=posts,
            note=f"query={primary_query!r}",
        ))

        time.sleep(random.uniform(*SLEEP_BETWEEN_QUERIES))

    return out
