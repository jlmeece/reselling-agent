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
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from loguru import logger

from .base_scout import Post, QueryContext, ScoutResult


SUB_SEARCH_URL = (
    "https://www.reddit.com/r/{sub}/search.json"
    "?q={query}&sort=new&limit=15&restrict_sr=1&t=month"
)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

def _get_headers() -> dict:
    return {"User-Agent": random.choice(_USER_AGENTS)}


REQUEST_TIMEOUT = 10
SLEEP_BETWEEN_QUERIES = (1.5, 3.0)

_REDDIT_BLOCKED = False


def _fetch(url: str, headers: dict | None = None) -> dict:
    try:
        req = urllib.request.Request(url, headers=headers or _get_headers())
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise
        logger.debug(f"  reddit fetch failed: {e}")
        return {}
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
    global _REDDIT_BLOCKED
    sub_clean = subreddit.lstrip("r/").lstrip("/").strip()

    if not _REDDIT_BLOCKED:
        url = SUB_SEARCH_URL.format(
            sub=urllib.parse.quote(sub_clean),
            query=urllib.parse.quote(query),
        )
        try:
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
            if posts:
                return posts
        except urllib.error.HTTPError as e:
            if e.code == 403:
                logger.warning("  reddit_scout: 403 — Reddit is blocking unauthenticated requests; switching all remaining subreddits to Pullpush")
                _REDDIT_BLOCKED = True
        except Exception:
            pass

    return _search_subreddit_pullpush(subreddit, query)


PULLPUSH_URL = "https://api.pullpush.io/reddit/search/submission/?q={query}&subreddit={sub}&size=10&sort=desc"

def _search_subreddit_pullpush(subreddit: str, query: str) -> list[Post]:
    sub_clean = subreddit.lstrip("r/").lstrip("/").strip()
    url = PULLPUSH_URL.format(
        sub=urllib.parse.quote(sub_clean),
        query=urllib.parse.quote(query),
    )
    data = _fetch(url, headers=_get_headers())
    posts = []
    source_id = f"reddit:r/{sub_clean}"
    for item in data.get("data", []):
        title = item.get("title")
        if not title:
            continue
        posts.append(Post.make(
            source_id=source_id,
            title=title,
            snippet=(item.get("selftext") or "")[:400],
            url=f"https://reddit.com{item.get('permalink', '')}",
            date_=_to_date(item.get("created_utc")),
            engagement=int(item.get("score", 0) or 0) + int(item.get("num_comments", 0) or 0),
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
