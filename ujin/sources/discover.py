"""Auto-discover RSS and sitemap URLs from a homepage.

Given a homepage URL we try, in order:
  1. <link rel="alternate" type="application/rss+xml"> (and atom+xml)
  2. /robots.txt — look for "Sitemap:" directives
  3. Well-known paths: /sitemap.xml, /sitemap_news.xml, /news-sitemap.xml, /feed, /rss

The caller decides what to do with the results. We return a typed
struct so callers can prefer RSS over sitemap when both are available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from ..fetch.http import HttpFetcher
from ..extract.links import normalize_url


@dataclass
class DiscoveredSources:
    homepage: str
    rss: list[str] = field(default_factory=list)
    sitemap: list[str] = field(default_factory=list)


_WELL_KNOWN_FEEDS = ("/feed", "/feed/", "/rss", "/rss.xml", "/index.xml")
_WELL_KNOWN_SITEMAPS = (
    "/sitemap.xml",
    "/sitemap_news.xml",
    "/news-sitemap.xml",
    "/news.xml",
)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _from_html(html: str, base: str) -> list[str]:
    tree = HTMLParser(html)
    out: list[str] = []
    for link in tree.css("link[rel=alternate]"):
        ftype = (link.attributes.get("type") or "").lower()
        href = link.attributes.get("href") or ""
        if not href:
            continue
        if "rss" in ftype or "atom" in ftype:
            absolute = normalize_url(href, base=base)
            if absolute:
                out.append(absolute)
    return out


def _from_robots(robots: str, origin: str) -> list[str]:
    out: list[str] = []
    for raw in robots.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "sitemap":
            absolute = normalize_url(value.strip(), base=origin)
            if absolute:
                out.append(absolute)
    return out


async def _head_or_get_ok(http: HttpFetcher, url: str) -> bool:
    """Probe a URL — return True if it likely exists (2xx).

    We do GET (not HEAD) because plenty of news CDNs return 405 or 403
    for HEAD even when GET works."""
    try:
        resp = await http.get(url)
    except Exception:  # noqa: BLE001 — discovery is best-effort
        return False
    return 200 <= resp.status < 300 and bool(resp.body.strip())


async def discover_sources(
    http: HttpFetcher, homepage: str
) -> DiscoveredSources:
    out = DiscoveredSources(homepage=homepage)
    origin = _origin(homepage)

    # 1. Pull homepage HTML once for <link rel=alternate> parsing.
    try:
        home_resp = await http.get(homepage)
        if home_resp.status == 200 and home_resp.body:
            out.rss.extend(_from_html(home_resp.body, base=homepage))
    except Exception:  # noqa: BLE001
        pass

    # 2. robots.txt sitemap directives.
    try:
        robots_resp = await http.get(urljoin(origin + "/", "/robots.txt"))
        if robots_resp.status == 200 and robots_resp.body:
            out.sitemap.extend(_from_robots(robots_resp.body, origin))
    except Exception:  # noqa: BLE001
        pass

    # 3. Well-known paths, but only probe ones we haven't already found.
    known_rss = set(out.rss)
    for path in _WELL_KNOWN_FEEDS:
        candidate = urljoin(origin + "/", path)
        if candidate in known_rss:
            continue
        if await _head_or_get_ok(http, candidate):
            out.rss.append(candidate)
            known_rss.add(candidate)

    known_sitemap = set(out.sitemap)
    for path in _WELL_KNOWN_SITEMAPS:
        candidate = urljoin(origin + "/", path)
        if candidate in known_sitemap:
            continue
        if await _head_or_get_ok(http, candidate):
            out.sitemap.append(candidate)
            known_sitemap.add(candidate)

    return out
