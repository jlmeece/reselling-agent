"""
Tool: community_signals
Adaptive multi-source community research orchestrator.

Iterates the scout registry, aggregates signal across sources weighted by each
source's accumulated performance, and persists per-source stats + per-product
research history to data/knowledge/.

The signal_strength returned here feeds the Tier 1+2 Claude prompt downstream.

Public API (preserves the previous `get_community_signals` signature so the
researcher doesn't need to change):

    get_community_signals(product_title, category, category_config, *,
                          brand=None, model=None, costco_url=None) -> dict

Returned dict keys:
    signal_strength:  float 0-10
    summary:          str — human-readable one-liner
    recent_posts:     list of post dicts (sorted by recency)
    source_breakdown: dict[source_id -> {mentions, freshness, intent_rate, weight, note}]
    intent_phrases:   list of detected intent phrases (for visibility)
    stale_sources:    list of source_ids flagged stale this run
    active_sources:   list of source_ids that yielded ≥1 post
"""

from __future__ import annotations

from datetime import date
from loguru import logger

from . import knowledge_store as ks
from .scouts import REGISTERED_SCOUTS
from .scouts.base_scout import QueryContext, ScoutResult, Post


NORMALIZATION = 12.0      # tunable — divides weighted-post sum into 0-10 scale
INTENT_BONUS  = 2.0       # multiplier applied to intent-tagged posts


# ── Aggregation ────────────────────────────────────────────────────────────────

def _post_weight(post: Post) -> float:
    base = INTENT_BONUS if post.intent_tags else 1.0
    fresh = _freshness_for_post(post.date)
    return base * fresh


def _freshness_for_post(post_date) -> float:
    if not post_date:
        return 0.3
    days = (date.today() - post_date).days
    if days <= 7:   return 1.0
    if days <= 30:  return 0.7
    if days <= 90:  return 0.3
    return 0.05


def _aggregate(scout_results: list[ScoutResult], sources_state: dict) -> float:
    total = 0.0
    for sr in scout_results:
        weight = sources_state.get(sr.source_id, {}).get("weight", 1.0)
        for p in sr.posts:
            total += _post_weight(p) * weight
    return round(min(max(total / NORMALIZATION, 0.0), 10.0), 2)


def _build_summary(scout_results: list[ScoutResult]) -> str:
    active = [sr for sr in scout_results if sr.mention_count > 0]
    if not active:
        return "No fresh community signal across registered sources."
    posts = [p for sr in active for p in sr.posts]
    intent_posts = [p for p in posts if p.intent_tags]
    newest = max(posts, key=lambda p: p.date or date.min)
    parts = [
        f"{len(posts)} posts across {len(active)} active source(s)",
        f"{len(intent_posts)} purchase-intent",
    ]
    if newest.date:
        parts.append(f'newest "{newest.title[:55]}" ({newest.source_id}, {(date.today()-newest.date).days}d ago)')
    return " | ".join(parts)


def _top_posts(scout_results: list[ScoutResult], n: int = 8) -> list[dict]:
    posts = [(sr.source_id, p) for sr in scout_results for p in sr.posts]
    intent_first = sorted(
        posts,
        key=lambda sp: (
            -bool(sp[1].intent_tags),
            -(sp[1].date.toordinal() if sp[1].date else 0),
        ),
    )
    out = []
    for src, p in intent_first[:n]:
        out.append({
            "source_id":   src,
            "title":       p.title,
            "url":         p.url,
            "date":        p.date.isoformat() if p.date else None,
            "engagement":  p.engagement,
            "intent_tags": p.intent_tags,
            "snippet":     p.snippet[:200],
        })
    return out


def _intent_phrases(scout_results: list[ScoutResult], limit: int = 6) -> list[str]:
    seen = []
    for sr in scout_results:
        for p in sr.posts:
            if p.intent_tags:
                seen.append(p.title[:80])
                if len(seen) >= limit:
                    return seen
    return seen


# ── Public API ─────────────────────────────────────────────────────────────────

def get_community_signals(product_title, category, category_config, *,
                          brand=None, model=None, costco_url=None):
    # Seed sources.json from yaml on first run / new entries
    # (no-op if already seeded; preserves existing stats)
    ks.ensure_seed_sources({category: category_config})

    query_ctx = QueryContext(
        title=product_title, category=category, brand=brand, model=model,
    )

    sources_state = ks.load_sources()
    scout_results: list[ScoutResult] = []

    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    _SCOUT_TIMEOUT = 90  # seconds per scout before giving up

    for scout_name, scout_module in REGISTERED_SCOUTS.items():
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(scout_module.run, query_ctx, category_config)
                results = fut.result(timeout=_SCOUT_TIMEOUT) or []
        except FuturesTimeout:
            logger.warning(f"  scout '{scout_name}' timed out after {_SCOUT_TIMEOUT}s — skipping")
            continue
        except Exception as e:
            logger.warning(f"  scout '{scout_name}' crashed: {e}")
            continue

        # Skip retired sources entirely
        results = [
            r for r in results
            if sources_state.get(r.source_id, {}).get("status") != "retired"
        ]
        scout_results.extend(results)

    signal_strength = _aggregate(scout_results, sources_state)

    # Persist per-source stats + candidate-domain promotion
    ks.update_after_run(scout_results, category)

    # Re-load to capture status transitions for breakdown
    sources_state = ks.load_sources()

    breakdown = {}
    active_sources, stale_sources = [], []
    for sr in scout_results:
        s = sources_state.get(sr.source_id, {})
        breakdown[sr.source_id] = {
            "mentions":   sr.mention_count,
            "freshness":  round(sr.freshness, 2),
            "intent_rate": sr.intent_rate,
            "weight":     s.get("weight", 1.0),
            "status":     s.get("status", "active"),
            "note":       sr.note,
        }
        (active_sources if sr.mention_count > 0 else stale_sources).append(sr.source_id)

    # Persist per-product run + bump keyword/link counts for top hits
    if product_title:
        slug = ks.slugify(product_title)
        ks.record_product_run(slug, {
            "title": product_title,
            "category": category,
            "costco_url": costco_url,
            "brand": brand,
            "model": model,
            "date": date.today().isoformat(),
            "signal_strength": signal_strength,
            "by_source": {sid: v["mentions"] for sid, v in breakdown.items()},
            "intent_phrases_seen": _intent_phrases(scout_results, limit=8),
            "top_links": [tp["url"] for tp in _top_posts(scout_results, n=5)],
        })
    for sr in scout_results:
        for p in sr.posts:
            ks.bump_link(p.url, sr.source_id, was_intent=bool(p.intent_tags))
            for token in p.title.lower().split():
                if len(token) >= 4 and token.isalpha():
                    ks.bump_keyword(category, token, was_intent=bool(p.intent_tags))

    summary = _build_summary(scout_results)
    recent_posts = _top_posts(scout_results)

    logger.info(
        f"  Community signal: {signal_strength}/10 | "
        f"{len(active_sources)} active / {len(stale_sources)} quiet sources"
    )

    return {
        "signal_strength":  signal_strength,
        "summary":          summary,
        "recent_posts":     recent_posts,
        "source_breakdown": breakdown,
        "intent_phrases":   _intent_phrases(scout_results),
        "stale_sources":    stale_sources,
        "active_sources":   active_sources,
    }
