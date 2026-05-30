"""RSS / Atom feed parsing via feedparser, off the event loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import feedparser


@dataclass
class FeedItem:
    url: str
    title: str
    summary: str
    published: Optional[str] = None


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
                published=(
                    entry.get("published")
                    or entry.get("updated")
                    or None
                ),
            )
        )
    return items
