"""Social sources: X/Twitter (chained), Mastodon, Truth Social, X trends.

All pluggable and independent of the poll engine. They surface via the scrape
service's ``/social/*`` and ``/trends/x`` routes, and can be wrapped in a
``CallablePollable`` for adaptive polling. Pulls the ``social`` extra
(aiohttp/selectolax/yaml — already covered by ``web``+``yaml``).
"""
from __future__ import annotations

from ._nitter import NitterPool, nitter_posts
from ._syndication import syndication_posts
from .mastodon import mastodon_timeline
from .truth import truth_social_posts
from .twitter import (
    BraveError,
    BraveNotConfigured,
    SocialPost,
    twitter_search,
)
from .x import ChainResult, x_posts
from .x_trends import TrendItem, TrendsResult, fetch_x_trends

__all__ = [
    "NitterPool",
    "nitter_posts",
    "syndication_posts",
    "mastodon_timeline",
    "truth_social_posts",
    "twitter_search",
    "BraveError",
    "BraveNotConfigured",
    "SocialPost",
    "x_posts",
    "ChainResult",
    "fetch_x_trends",
    "TrendItem",
    "TrendsResult",
]
