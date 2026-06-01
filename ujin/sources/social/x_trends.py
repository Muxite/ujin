"""X/Twitter trending topics — scraped from public dashboards.

Primary:  trends24.in/<region>/
Fallback: getdaytrends.com/<region>/

Both return human-readable HTML; we extract the rank-ordered tag list.
Cached at the route layer with a 5-min TTL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp
from selectolax.parser import HTMLParser

logger = logging.getLogger("ujin.sources.social.x_trends")

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; jennie-scraper-v2/1.0)",
    "Accept": "text/html",
}


@dataclass
class TrendItem:
    rank: int
    tag: str
    url: Optional[str] = None
    volume: Optional[str] = None


@dataclass
class TrendsResult:
    region: str
    items: list[TrendItem]
    source: str  # "trends24" | "getdaytrends" | "empty"


async def fetch_x_trends(region: str = "united-states", count: int = 20) -> TrendsResult:
    region = (region or "united-states").strip().strip("/")
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS) as session:
        items = await _from_trends24(session, region, count)
        if items:
            return TrendsResult(region=region, items=items, source="trends24")
        items = await _from_getdaytrends(session, region, count)
        if items:
            return TrendsResult(region=region, items=items, source="getdaytrends")
    return TrendsResult(region=region, items=[], source="empty")


async def _from_trends24(
    session: aiohttp.ClientSession, region: str, count: int
) -> list[TrendItem]:
    url = f"https://trends24.in/{region}/"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()
    except Exception as exc:  # noqa: BLE001
        logger.debug("trends24 %s err: %s", region, exc)
        return []
    tree = HTMLParser(html)
    # First trend card holds the freshest snapshot.
    card = tree.css_first(".trend-card .trend-card__list")
    if card is None:
        card = tree.css_first(".trend-card__list")
    if card is None:
        return []
    out: list[TrendItem] = []
    for i, li in enumerate(card.css("li"), start=1):
        a = li.css_first("a")
        if a is None:
            continue
        tag = (a.text() or "").strip()
        if not tag:
            continue
        href = a.attributes.get("href") or None
        vol_node = li.css_first(".tweet-count")
        vol = (vol_node.text() or "").strip() if vol_node is not None else None
        out.append(TrendItem(rank=i, tag=tag, url=href, volume=vol))
        if len(out) >= count:
            break
    return out


async def _from_getdaytrends(
    session: aiohttp.ClientSession, region: str, count: int
) -> list[TrendItem]:
    url = f"https://getdaytrends.com/{region}/"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()
    except Exception as exc:  # noqa: BLE001
        logger.debug("getdaytrends %s err: %s", region, exc)
        return []
    tree = HTMLParser(html)
    out: list[TrendItem] = []
    for i, row in enumerate(tree.css("table.ranking tbody tr"), start=1):
        a = row.css_first("a.trend-link, a")
        if a is None:
            continue
        tag = (a.text() or "").strip()
        if not tag:
            continue
        out.append(TrendItem(rank=i, tag=tag, url=a.attributes.get("href")))
        if len(out) >= count:
            break
    return out
