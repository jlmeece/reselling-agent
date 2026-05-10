"""
tools.scouts.base_scout
Shared types + helpers used by every scout (reddit / youtube / forum / web).

The orchestrator (`tools.community_signals`) imports a scout module and calls
its `run(query_ctx, category_config)` function. Every scout returns a single
ScoutResult.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional


# ── Intent tagging ─────────────────────────────────────────────────────────────
# Patterns are intentionally broad. False positives cost less than false negatives
# in early discovery. Each tag fires independently — a single post can carry
# multiple tags.

INTENT_PATTERNS = {
    "where_to_buy": [
        r"\bwhere\s+(can|do|should)\s+i\s+(buy|find|get|purchase)\b",
        r"\blooking\s+(for|to\s+buy)\b",
        r"\banyone\s+selling\b",
        r"\bin\s+stock\b",
        r"\bbest\s+place\s+to\s+buy\b",
    ],
    "is_legit": [
        r"\bis\s+.{0,40}?\s*legit\b",
        r"\bis\s+.{0,40}?\s*real\b",
        r"\bfake\s+or\s+real\b",
        r"\bauthentic(ity)?\b",
        r"\bcounterfeit\b",
    ],
    "is_worth": [
        r"\bworth\s+it\b",
        r"\bgood\s+(investment|buy|deal)\b",
        r"\bshould\s+i\s+buy\b",
        r"\bworth\s+the\s+(money|price)\b",
    ],
    "review": [
        r"\breview\b",
        r"\bafter\s+\d+\s+(days|weeks|months|years)\b",
        r"\bi\s+(bought|purchased|ordered)\b",
        r"\bunboxing\b",
    ],
    "resale": [
        r"\bflipping\b",
        r"\bresell(ing)?\b",
        r"\b(ebay|amazon|mercari)\s+(margin|profit|fees)\b",
        r"\bprofit\s+on\b",
    ],
}

_COMPILED_INTENT = {
    tag: [re.compile(p, re.IGNORECASE) for p in pats]
    for tag, pats in INTENT_PATTERNS.items()
}


def detect_intent_tags(text: str) -> list[str]:
    """Return list of intent tags whose patterns match the given text."""
    if not text:
        return []
    tags = []
    for tag, regexes in _COMPILED_INTENT.items():
        if any(rx.search(text) for rx in regexes):
            tags.append(tag)
    return tags


# ── Freshness ──────────────────────────────────────────────────────────────────

def freshness_from_latest(latest: Optional[date]) -> float:
    """0.0–1.0 based on days since the most recent matching post."""
    if not latest:
        return 0.0
    if isinstance(latest, datetime):
        latest = latest.date()
    days = (date.today() - latest).days
    if days < 0:    return 1.0
    if days <= 7:   return 1.0
    if days <= 30:  return 0.7
    if days <= 90:  return 0.3
    return 0.0


# ── Data shapes ────────────────────────────────────────────────────────────────

@dataclass
class Post:
    title:        str
    url:          str
    date:         Optional[date]
    snippet:      str
    engagement:   int                       # upvotes / comments / view-equivalents
    intent_tags:  list[str] = field(default_factory=list)
    source_id:    str = ""

    @classmethod
    def make(cls, *, title: str, url: str, date_=None, snippet: str = "",
             engagement: int = 0, source_id: str = "") -> "Post":
        text = f"{title} {snippet}"
        return cls(
            title=title[:200],
            url=url,
            date=date_,
            snippet=snippet[:400],
            engagement=engagement,
            intent_tags=detect_intent_tags(text),
            source_id=source_id,
        )


@dataclass
class ScoutResult:
    source_id:         str
    source_type:       str           # "reddit" | "youtube" | "forum" | "web"
    posts:             list[Post]    = field(default_factory=list)
    freshness:         float         = 0.0
    intent_rate:       float         = 0.0
    mention_count:     int           = 0
    candidate_domains: list[str]     = field(default_factory=list)
    note:              str           = ""

    @classmethod
    def from_posts(cls, source_id: str, source_type: str, posts: list[Post],
                   candidate_domains: Optional[list[str]] = None,
                   note: str = "") -> "ScoutResult":
        if posts:
            latest = max((p.date for p in posts if p.date), default=None)
            freshness = freshness_from_latest(latest)
            intent_n  = sum(1 for p in posts if p.intent_tags)
            intent_rate = intent_n / len(posts)
        else:
            freshness, intent_rate = 0.0, 0.0
        return cls(
            source_id=source_id,
            source_type=source_type,
            posts=posts,
            freshness=freshness,
            intent_rate=round(intent_rate, 3),
            mention_count=len(posts),
            candidate_domains=candidate_domains or [],
            note=note,
        )


# ── Query context ──────────────────────────────────────────────────────────────

@dataclass
class QueryContext:
    title:    str
    category: str
    brand:    Optional[str] = None
    model:    Optional[str] = None

    def best_query(self) -> str:
        """
        Prefer brand+model when available — much higher signal-to-noise than
        marketing title tokens.
        """
        if self.brand and self.model:
            return f"{self.brand} {self.model}".strip()
        if self.brand:
            return f"{self.brand} {self.title}".strip()
        return self.title

    def render_template(self, template: str) -> str:
        """Replace {brand}, {model}, {title} placeholders. Empty placeholders dropped."""
        out = (template
               .replace("{brand}", (self.brand or "").strip())
               .replace("{model}", (self.model or "").strip())
               .replace("{title}", self.title))
        return re.sub(r"\s+", " ", out).strip()
