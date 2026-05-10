"""
tools.scouts.forum_scout
Niche forum scout — Playwright-based, uses the existing CDP-connected Chrome
session (same one costco_scraper / ebay_research use). Logged-in profile helps
bypass Cloudflare-style challenges that block headless browsers.

Strategy: per forum entry in `category_config.community_sources.forums`, build
the search URL from `{url}{search_path}` with `{query}` interpolated, navigate,
and extract title/snippet/date from generic forum-result selectors.

If a forum's search page doesn't match any of our known patterns, the scout
emits an empty ScoutResult with a note (so the knowledge_store records the
miss and can demote/retire after enough dry runs).
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import date, datetime, timedelta

from loguru import logger
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .base_scout import Post, QueryContext, ScoutResult


# Selectors to try, in order. First non-empty match wins.
RESULT_SELECTORS = [
    ".structItem-title a",          # XenForo (kitco, bullionstacker, watchuseek)
    "h3.contentRow-title a",
    ".searchResult h3 a",
    "li.b-threadlist__item .threadtitle a",  # vBulletin
    ".topictitle",                  # phpBB
    "a.searchResultTitle",
    "h3 a, h2 a",                   # generic fallback (loose, last)
]

DATE_TEXT_SELECTORS = [
    ".structItem-startDate time",
    "time[datetime]",
    ".message-date",
    "abbr.published",
    ".created",
]

REL_DATE_PATTERNS = [
    (re.compile(r"(\d+)\s*minute"), "minutes"),
    (re.compile(r"(\d+)\s*hour"),   "hours"),
    (re.compile(r"(\d+)\s*day"),    "days"),
    (re.compile(r"(\d+)\s*week"),   "weeks"),
    (re.compile(r"(\d+)\s*month"),  "months"),
    (re.compile(r"(\d+)\s*year"),   "years"),
]


def _parse_date_string(s: str) -> date | None:
    if not s:
        return None
    s = s.strip()

    # Try ISO 8601 in datetime attribute first
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        pass

    # Common forum formats
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    # Relative ("3 days ago", "2 weeks ago")
    s_lower = s.lower()
    for rx, unit in REL_DATE_PATTERNS:
        m = rx.search(s_lower)
        if m:
            n = int(m.group(1))
            kwargs = {unit: n} if unit != "months" else {"days": n * 30}
            if unit == "years":
                kwargs = {"days": n * 365}
            return (datetime.now() - timedelta(**kwargs)).date()

    return None


def _absolutize(base_url: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("/"):
        from urllib.parse import urlparse
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{href}"
    return f"{base_url.rstrip('/')}/{href}"


def _scrape_forum(page, base_url: str, search_path: str, query: str,
                  source_id: str, max_results: int = 10) -> list[Post]:
    if "{query}" not in search_path:
        search_path = search_path + ("&" if "?" in search_path else "?") + f"q={{query}}"
    full_url = base_url.rstrip("/") + search_path.replace("{query}", urllib.parse.quote(query))

    posts: list[Post] = []
    try:
        page.goto(full_url, timeout=20000, wait_until="domcontentloaded")
    except PlaywrightTimeout:
        logger.debug(f"  forum_scout {source_id}: timeout loading search page")
        return posts
    except Exception as e:
        logger.debug(f"  forum_scout {source_id}: nav failed: {e}")
        return posts

    page.wait_for_timeout(800)

    title_links = []
    for sel in RESULT_SELECTORS:
        title_links = page.query_selector_all(sel)
        if title_links:
            break

    if not title_links:
        return posts

    for el in title_links[:max_results]:
        try:
            title = (el.inner_text() or "").strip()
            href  = el.get_attribute("href") or ""
        except Exception:
            continue
        if not title or len(title) < 5:
            continue

        url = _absolutize(base_url, href)

        # Try to walk up to a row container and extract date + snippet
        row = el
        for _ in range(4):
            try:
                row = row.evaluate_handle("el => el.parentElement").as_element()
            except Exception:
                break
            if row is None:
                break

        post_date = None
        snippet = ""
        if row is not None:
            for ds in DATE_TEXT_SELECTORS:
                try:
                    de = row.query_selector(ds)
                except Exception:
                    de = None
                if not de:
                    continue
                attr = de.get_attribute("datetime") or de.get_attribute("title") or ""
                txt  = (de.inner_text() or "").strip()
                post_date = _parse_date_string(attr) or _parse_date_string(txt)
                if post_date:
                    break
            try:
                snippet = (row.inner_text() or "")[:300]
            except Exception:
                snippet = ""

        posts.append(Post.make(
            title=title,
            url=url,
            date_=post_date,
            snippet=snippet,
            engagement=0,
            source_id=source_id,
        ))

    return posts


def run(query_ctx: QueryContext, category_config: dict) -> list[ScoutResult]:
    cs = category_config.get("community_sources", {})
    if not isinstance(cs, dict):
        return []
    forums = cs.get("forums", []) or []
    if not forums:
        return []

    # Lazy CDP connection — orchestrator passes us nothing about Chrome, so we
    # open a CDP session here. costco_scraper.make_browser is a contextmanager
    # but spinning a separate one per scout step would be wasteful; so we use
    # the simpler _ensure_chrome + direct connect.
    try:
        from playwright.sync_api import sync_playwright
        from tools.costco_scraper import _ensure_chrome
        _ensure_chrome()
        pw = sync_playwright().start()
    except Exception as e:
        logger.warning(f"  forum_scout: playwright init failed: {e}")
        return []

    out: list[ScoutResult] = []
    try:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        query = query_ctx.best_query()

        for entry in forums:
            if not isinstance(entry, dict):
                continue
            base_url = entry.get("url", "").rstrip("/")
            search_path = entry.get("search_path", "/search?q={query}")
            if not base_url:
                continue

            from urllib.parse import urlparse
            host = urlparse(base_url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            source_id = f"forum:{host}"

            try:
                posts = _scrape_forum(page, base_url, search_path, query, source_id)
            except Exception as e:
                logger.warning(f"  forum_scout {source_id} crashed: {e}")
                posts = []

            note = f"query={query!r}" if posts else "no results matched known selectors"
            out.append(ScoutResult.from_posts(
                source_id=source_id,
                source_type="forum",
                posts=posts,
                note=note,
            ))

        page.close()
    finally:
        try:
            pw.stop()
        except Exception:
            pass

    return out
