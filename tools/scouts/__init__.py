"""
tools.scouts
Registry of scout modules. The orchestrator iterates over REGISTERED_SCOUTS;
each entry is a (source_type, module) pair.

Adding a scout: implement a module exposing `run(query_ctx, category_config) -> list[ScoutResult]`
(plural — a single scout module may produce multiple results, e.g. one per
subreddit), then register it here.
"""

from . import reddit_scout, youtube_scout, forum_scout, web_scout

REGISTERED_SCOUTS = {
    "reddit":  reddit_scout,
    "youtube": youtube_scout,
    "forum":   forum_scout,
    "web":     web_scout,
}
