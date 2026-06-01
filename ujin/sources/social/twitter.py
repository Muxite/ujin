"""Twitter/X via Brave Search API.

Brave indexes x.com/twitter.com and we extract post URLs from web results.
Direct X API is unavailable / paid.

The Brave subscription token is read from the ``api_key`` argument, falling
back to the ``SEARCH_API_KEY`` environment variable (the scrape service threads
``ScrapeConfig.brave_api_key`` in via the route layer).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import aiohttp


@dataclass
class SocialPost:
    url: str
    text: str


class BraveNotConfigured(RuntimeError):
    pass


class BraveError(RuntimeError):
    pass


def _resolve_key(api_key: str | None) -> str:
    return api_key if api_key is not None else os.environ.get("SEARCH_API_KEY", "")


async def twitter_search(
    username: str, count: int = 10, *, api_key: str | None = None
) -> list[SocialPost]:
    key = _resolve_key(api_key)
    if not key:
        raise BraveNotConfigured("SEARCH_API_KEY not configured")

    username = username.lstrip("@").strip()
    query = f"from:{username} site:x.com OR site:twitter.com"

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={
                "q": query,
                "count": min(max(count, 1), 20),
                "search_lang": "en",
            },
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": key,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise BraveError(
                    f"Brave API {resp.status}: {text[:200]}"
                )
            data = await resp.json()

    posts: list[SocialPost] = []
    for result in data.get("web", {}).get("results", []):
        url = result.get("url", "") or ""
        if not url:
            continue
        title = result.get("title", "") or ""
        description = result.get("description", "") or ""
        posts.append(SocialPost(url=url, text=f"{title} {description}".strip()))
    return posts
