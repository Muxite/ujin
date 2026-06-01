"""X/Twitter syndication endpoint — public, no auth.

`cdn.syndication.twimg.com/timeline/profile` is the JSON Twitter uses
for the public embed widgets. It returns the last ~25 visible posts
for a public account. No API key required. Brittle — Twitter has
shipped breaking changes here every 6–12 months; we treat parse
failures as silent "mirror down" and let the chain fall through.

Response shape (abbreviated, as of 2026-05-26):
  {
    "body": "<html...>...</html>",      # legacy
    "head": "...",
    "timeline": {
      "entries": [
        {
          "type": "tweet",
          "content": {"tweet": {
            "id": "1...",
            "full_text": "...",
            "permalink_url": "https://twitter.com/...",
            ...
          }}
        }
      ]
    }
  }

We accept either the structured `timeline.entries` form or the older
`body` HTML form (re-parsed via selectolax) for resilience.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import aiohttp
from selectolax.parser import HTMLParser

from .twitter import SocialPost

logger = logging.getLogger("ujin.sources.social.x_syndication")

_BASE = "https://cdn.syndication.twimg.com/timeline/profile"
_TIMEOUT = aiohttp.ClientTimeout(total=10)
_PERMALINK_RE = re.compile(r"^https?://(?:x|twitter)\.com/[^/]+/status/\d+")


async def syndication_posts(
    username: str, count: int = 20, *, session: Optional[aiohttp.ClientSession] = None
) -> list[SocialPost]:
    username = username.lstrip("@").strip()
    if not username:
        return []
    params = {
        "screen_name": username,
        "showReplies": "false",
        "with_replies": "false",
    }
    headers = {
        "Accept": "application/json, text/html;q=0.5",
        "User-Agent": "Mozilla/5.0 (compatible; jennie-scraper-v2/1.0)",
    }
    own = session is None
    if own:
        session = aiohttp.ClientSession(timeout=_TIMEOUT, headers=headers)
    try:
        async with session.get(_BASE, params=params, headers=headers) as resp:
            if resp.status != 200:
                logger.debug("syndication %s -> HTTP %s", username, resp.status)
                return []
            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                data = await resp.json(content_type=None)
                return _from_json(data, count)
            text = await resp.text()
            try:
                import json
                data = json.loads(text)
                if isinstance(data, dict):
                    return _from_json(data, count)
            except Exception:
                pass
            return _from_html(text, count)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.debug("syndication %s err: %s", username, exc)
        return []
    finally:
        if own:
            await session.close()


def _from_json(data: dict, count: int) -> list[SocialPost]:
    out: list[SocialPost] = []
    timeline = data.get("timeline") or {}
    entries = timeline.get("entries") or []
    for entry in entries:
        if entry.get("type") != "tweet":
            continue
        tweet = (entry.get("content") or {}).get("tweet") or {}
        text = (tweet.get("full_text") or tweet.get("text") or "").strip()
        permalink = tweet.get("permalink_url") or tweet.get("permalink") or ""
        if not permalink or not text:
            continue
        if not _PERMALINK_RE.match(permalink):
            continue
        out.append(SocialPost(url=permalink, text=text))
        if len(out) >= count:
            break
    if not out:
        body = data.get("body")
        if isinstance(body, str):
            return _from_html(body, count)
    return out


def _from_html(html: str, count: int) -> list[SocialPost]:
    if not html:
        return []
    tree = HTMLParser(html)
    out: list[SocialPost] = []
    for tweet in tree.css("[class*=Tweet]"):
        text_node = tweet.css_first(".timeline-Tweet-text, .Tweet-text, p")
        link_node = tweet.css_first("a[href*='/status/']")
        if text_node is None or link_node is None:
            continue
        href = link_node.attributes.get("href", "") or ""
        if not _PERMALINK_RE.match(href):
            continue
        text = (text_node.text() or "").strip()
        if not text:
            continue
        out.append(SocialPost(url=href, text=text))
        if len(out) >= count:
            break
    return out
