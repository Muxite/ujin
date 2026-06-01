"""Alternate-path fallback chain.

Some news sites (wsj, nyt, ft, washingtonpost, thehill, ...) return
403 to plain HTTP and even to obscura because their anti-bot layer
fingerprints the renderer. But the same sites usually leave
news-sitemaps and RSS unprotected, because those are how Google News
finds them. This module walks a fallback chain when the primary
fetch path can't extract headlines.

Strategies, in order:
  1. sitemap_news  — try /news-sitemap.xml + variants, synthesize links
  2. rss           — use a previously-discovered RSS for this host

Each strategy returns either a list of NormalizedLink or None.
ScrapeService walks the chain and uses the first that produces links.
The strategy name flows back via ScrapeResult.strategy_used so the
audit can see which path actually worked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from ..extract.links import NormalizedLink, normalize_url
from ..sources.sitemap import parse_sitemap_xml


_SITEMAP_PATHS = (
    "/news-sitemap.xml",
    "/sitemap_news.xml",
    "/sitemap-news.xml",
    "/news.xml",
    "/sitemap.xml",  # last resort; large generic sitemaps
)


@dataclass
class AltPathResult:
    strategy: str
    links: list[NormalizedLink]


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _is_article_url(url: str) -> bool:
    """Heuristic: article URLs have a path with multiple segments and
    often a numeric segment (year or id). Homepages don't."""
    parts = urlsplit(url)
    path = parts.path.strip("/")
    if not path:
        return False
    segments = path.split("/")
    if len(segments) < 2:
        return False
    # Any segment that's all-digits looks like a date or id.
    return any(seg.isdigit() for seg in segments) or any(
        len(seg) > 12 for seg in segments
    )


async def try_sitemap_news(http, url: str) -> Optional[AltPathResult]:
    """Probe well-known news-sitemap paths. Returns links if any path
    responds with parseable XML containing <url> entries.

    `http` must be an HttpFetcher-shaped object (has async `get(url)`).
    """
    origin = _origin(url)
    for path in _SITEMAP_PATHS:
        candidate = origin.rstrip("/") + path
        try:
            resp = await http.get(candidate)
        except Exception:
            continue
        if resp.status != 200 or not resp.body:
            continue
        # Quick sanity check before parsing: must contain <url> or <urlset>.
        body_lower = resp.body[:1024].lower()
        if "<urlset" not in body_lower and "<url>" not in body_lower:
            continue
        entries = parse_sitemap_xml(resp.body)
        # Skip sitemap-index files (entries pointing at other XMLs).
        articles = [
            e for e in entries
            if e.url and not e.url.rstrip("/").endswith(".xml")
        ]
        if not articles:
            continue
        # Cap to the most recent 200; sitemaps can be huge.
        seen: dict[str, NormalizedLink] = {}
        for e in articles[:200]:
            canon = normalize_url(e.url, base=origin)
            if not canon or canon in seen:
                continue
            seen[canon] = NormalizedLink(url=canon, text=(e.title or "").strip())
        if seen:
            return AltPathResult(strategy="sitemap_news", links=list(seen.values()))
    return None


async def try_rss_fallback(
    rss_url: Optional[str], parse_feed_fn
) -> Optional[AltPathResult]:
    """If a discovered RSS URL exists for this host, use it.

    `parse_feed_fn` is injected so this module doesn't have to import
    feedparser at the top level (tests mock it). Signature:
    `async def parse_feed_fn(url: str) -> list[FeedItem]`.
    """
    if not rss_url:
        return None
    try:
        items = await parse_feed_fn(rss_url)
    except Exception:
        return None
    if not items:
        return None
    links: list[NormalizedLink] = []
    seen: set[str] = set()
    for item in items:
        canon = normalize_url(item.url) or item.url
        if not canon or canon in seen:
            continue
        seen.add(canon)
        links.append(NormalizedLink(url=canon, text=(item.title or "").strip()))
    if not links:
        return None
    return AltPathResult(strategy="rss", links=links)
