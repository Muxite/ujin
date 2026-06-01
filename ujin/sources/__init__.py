"""Source discovery + parsing: RSS/Atom feeds, sitemaps, auto-discovery.

Feed parsing pulls ``feedparser`` (``web`` extra); sitemap/discover use the
HTTP client. Social sources live in :mod:`ujin.sources.social`.
"""
from __future__ import annotations

from .discover import DiscoveredSources, discover_sources
from .rss import FeedItem, parse_feed
from .sitemap import SitemapEntry, fetch_sitemap, parse_sitemap_xml

__all__ = [
    "FeedItem",
    "parse_feed",
    "DiscoveredSources",
    "discover_sources",
    "SitemapEntry",
    "parse_sitemap_xml",
    "fetch_sitemap",
]
