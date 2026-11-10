"""RSS / Atom feed parsing via feedparser, off the event loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser


@dataclass
class FeedItem:
    url: str
    title: str
    summary: str
    published: Optional[str] = None


def _iso_published(entry) -> Optional[str]:
    """Entry date as ISO-8601 UTC; the raw feed string only if feedparser can't parse it."""
    for parsed_key, raw_key in (("published_parsed", "published"), ("updated_parsed", "updated")):
        parsed = entry.get(parsed_key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
        raw = entry.get(raw_key)
        if raw:
            return raw
    return None


async def parse_feed(url: str, *, timeout_secs: int = 20) -> list[FeedItem]:
    loop = asyncio.get_running_loop()
    parsed = await asyncio.wait_for(
        loop.run_in_executor(None, feedparser.parse, url),
        timeout=timeout_secs,
    )
    items: list[FeedItem] = []
    for entry in getattr(parsed, "entries", []) or []:
        link = entry.get("link", "") or ""
        if not link:
            continue
        items.append(
            FeedItem(
                url=link,
                title=entry.get("title", "") or "",
                summary=entry.get("summary", "") or "",
                published=_iso_published(entry),
            )
        )
    return items
