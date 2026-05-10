"""
tools.scouts.youtube_scout
YouTube Data API v3 — searches recent videos for product queries, then pulls
top comments for intent tagging.

Cost per product (rough):
  - 1 search.list call per query template:    100 units
  - 1 commentThreads.list per top video (×3): 1 unit each
  Total: ~100-130 units. Daily free quota: 10,000 → ~70 products/day comfortably.

Requires YOUTUBE_API_KEY in .env. If missing, the scout returns an empty result
(graceful degradation — orchestrator continues with other sources).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from loguru import logger

from .base_scout import Post, QueryContext, ScoutResult


VIDEOS_PER_QUERY     = 5
COMMENTS_PER_VIDEO   = 30
PUBLISHED_AFTER_DAYS = 90
MAX_TEMPLATES        = 3        # cap per product to keep quota bounded


def _client():
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        return None
    try:
        from googleapiclient.discovery import build
        return build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    except Exception as e:
        logger.warning(f"  youtube_scout: client init failed: {e}")
        return None


def _published_after_iso() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=PUBLISHED_AFTER_DAYS)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_yt_date(iso: str) -> date | None:
    if not iso:
        return None
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _search_videos(yt, query: str) -> list[dict]:
    try:
        req = yt.search().list(
            q=query,
            type="video",
            part="snippet",
            order="relevance",
            maxResults=VIDEOS_PER_QUERY,
            publishedAfter=_published_after_iso(),
            relevanceLanguage="en",
        )
        return req.execute().get("items", [])
    except Exception as e:
        logger.debug(f"  youtube search '{query}' failed: {e}")
        return []


def _top_comments(yt, video_id: str) -> list[dict]:
    try:
        req = yt.commentThreads().list(
            videoId=video_id,
            part="snippet",
            order="relevance",
            maxResults=COMMENTS_PER_VIDEO,
            textFormat="plainText",
        )
        return req.execute().get("items", [])
    except Exception as e:
        # Comments often disabled on a video — debug-level only
        logger.debug(f"  youtube comments {video_id} failed: {e}")
        return []


def run(query_ctx: QueryContext, category_config: dict) -> list[ScoutResult]:
    yt = _client()
    if yt is None:
        return []

    cs = category_config.get("community_sources", {})
    if not isinstance(cs, dict):
        return []
    templates = cs.get("youtube_query_templates", []) or []
    if not templates:
        return []

    rendered_queries = []
    for t in templates[:MAX_TEMPLATES]:
        q = query_ctx.render_template(t)
        if q and q not in rendered_queries:
            rendered_queries.append(q)
    if not rendered_queries:
        return []

    posts: list[Post] = []
    seen_video_ids: set[str] = set()
    newest_video_date: date | None = None

    for query in rendered_queries:
        videos = _search_videos(yt, query)
        for v in videos:
            vid = v.get("id", {}).get("videoId")
            if not vid or vid in seen_video_ids:
                continue
            seen_video_ids.add(vid)
            sn = v.get("snippet", {}) or {}
            v_date = _parse_yt_date(sn.get("publishedAt", ""))
            if v_date and (newest_video_date is None or v_date > newest_video_date):
                newest_video_date = v_date

            # Treat the video itself as one post (title + description for intent matching)
            posts.append(Post.make(
                title=sn.get("title", "")[:200],
                url=f"https://www.youtube.com/watch?v={vid}",
                date_=v_date,
                snippet=sn.get("description", "")[:400],
                engagement=0,           # video view counts cost extra quota; skip
                source_id="youtube:search",
            ))

            # Add top comments as additional posts (high signal for intent)
            for c in _top_comments(yt, vid):
                top_snippet = (
                    c.get("snippet", {})
                     .get("topLevelComment", {})
                     .get("snippet", {})
                ) or {}
                text = top_snippet.get("textDisplay") or top_snippet.get("textOriginal") or ""
                if not text.strip():
                    continue
                posts.append(Post.make(
                    title=text[:140],   # comments don't have titles; first line stands in
                    url=f"https://www.youtube.com/watch?v={vid}",
                    date_=_parse_yt_date(top_snippet.get("publishedAt", "")),
                    snippet=text[:400],
                    engagement=int(top_snippet.get("likeCount", 0) or 0),
                    source_id="youtube:comment",
                ))

    if not posts:
        return [ScoutResult(
            source_id="youtube:search",
            source_type="youtube",
            note=f"queries={rendered_queries!r} returned nothing",
        )]

    return [ScoutResult.from_posts(
        source_id="youtube:search",
        source_type="youtube",
        posts=posts,
        note=f"{len(seen_video_ids)} videos, {len(posts)} posts (videos+comments)",
    )]
