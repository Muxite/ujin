"""Truth Social via the per-user public RSS feed.

Same shape as v1 (delegates to feedparser) but exposed under a typed
function rather than letting the caller hand-craft the URL.
"""

from __future__ import annotations

from ..rss import parse_feed
from .twitter import SocialPost


_FEED_TEMPLATE = "https://truthsocial.com/@{username}/feed.rss"


async def truth_social_posts(username: str, count: int = 20) -> list[SocialPost]:
    username = username.lstrip("@").strip()
    feed_url = _FEED_TEMPLATE.format(username=username)
    items = await parse_feed(feed_url)
    posts: list[SocialPost] = []
    for item in items[: max(1, count)]:
        text = item.title
        if item.summary and item.summary != item.title:
            text = f"{item.title} {item.summary}".strip()
        posts.append(SocialPost(url=item.url, text=text))
    return posts
