"""Mastodon public timeline / account fetcher.

Mastodon servers expose a public JSON API with no auth needed for
public timelines:
  GET /api/v1/accounts/lookup?acct=<username>     → account info
  GET /api/v1/accounts/<id>/statuses              → recent statuses

We do the two-step lookup so callers can pass a bare `@user@instance`
form instead of needing to know the numeric account ID.

The User-Agent is taken from ``user_agent`` (the route threads
``ScrapeConfig.user_agent``), falling back to ``SCRAPER_USER_AGENT`` env.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from .twitter import SocialPost

_DEFAULT_UA = "Mozilla/5.0 (compatible; ujin-scrape/0.3)"
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    return _WHITESPACE.sub(" ", _HTML_TAG.sub(" ", html or "")).strip()


def _resolve_ua(user_agent: str | None) -> str:
    if user_agent:
        return user_agent
    return os.environ.get("SCRAPER_USER_AGENT", _DEFAULT_UA)


@dataclass
class _Account:
    instance: str
    user: str


def _split_account(account: str) -> _Account:
    """`@user@instance.tld` or `user@instance.tld` → (instance, user)."""
    s = account.lstrip("@").strip()
    if "@" not in s:
        raise ValueError(f"Mastodon account must include @instance: {account!r}")
    user, _, instance = s.partition("@")
    if not user or not instance:
        raise ValueError(f"Invalid Mastodon account: {account!r}")
    return _Account(instance=instance.lower(), user=user)


async def mastodon_timeline(
    account: str, count: int = 20, *, user_agent: str | None = None
) -> list[SocialPost]:
    acct = _split_account(account)
    base = f"https://{acct.instance}"

    headers = {
        "User-Agent": _resolve_ua(user_agent),
        "Accept": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(
            f"{base}/api/v1/accounts/lookup",
            params={"acct": acct.user},
        ) as resp:
            if resp.status != 200:
                return []
            account_data = await resp.json()

        account_id = account_data.get("id")
        if not account_id:
            return []

        async with session.get(
            f"{base}/api/v1/accounts/{quote(str(account_id))}/statuses",
            params={
                "limit": min(max(count, 1), 40),
                "exclude_replies": "true",
                "exclude_reblogs": "false",
            },
        ) as resp:
            if resp.status != 200:
                return []
            statuses = await resp.json()

    posts: list[SocialPost] = []
    for status in statuses or []:
        url = status.get("url") or ""
        if not url:
            continue
        text = _strip_html(status.get("content") or "")
        if not text and status.get("reblog"):
            # Reblog of someone else's post.
            inner = status["reblog"]
            text = _strip_html(inner.get("content") or "")
            url = inner.get("url") or url
        posts.append(SocialPost(url=url, text=text))
    return posts
