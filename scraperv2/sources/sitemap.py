"""sitemap.xml parsing.

Many news orgs publish a `news-sitemap.xml` or per-day sitemap that
lists recent stories with publish timestamps. Where it exists this is
the most efficient way to track new headlines — no JS rendering, no
boilerplate, just a flat list with metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from selectolax.parser import HTMLParser

from ..fetch.http import HttpFetcher


@dataclass
class SitemapEntry:
    url: str
    lastmod: Optional[str] = None
    title: Optional[str] = None


_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_LASTMOD = re.compile(r"<lastmod>\s*([^<]+?)\s*</lastmod>", re.IGNORECASE)
_NEWS_TITLE = re.compile(
    r"<news:title>\s*([^<]+?)\s*</news:title>", re.IGNORECASE
)


def parse_sitemap_xml(xml: str) -> list[SitemapEntry]:
    """Cheap regex parser — works fine because sitemap XML is simple,
    and selectolax doesn't natively handle XML namespaces well."""
    if not xml:
        return []
    # Split into <url> blocks so we can pair loc with lastmod.
    blocks = re.split(r"<url\b", xml, flags=re.IGNORECASE)[1:]
    entries: list[SitemapEntry] = []
    for block in blocks:
        loc_match = _LOC.search(block)
        if not loc_match:
            continue
        lastmod_match = _LASTMOD.search(block)
        title_match = _NEWS_TITLE.search(block)
        entries.append(
            SitemapEntry(
                url=loc_match.group(1),
                lastmod=lastmod_match.group(1) if lastmod_match else None,
                title=title_match.group(1) if title_match else None,
            )
        )
    # If there were no <url> wrappers, treat the file as a sitemap index
    # and surface those nested sitemap URLs.
    if not entries:
        for m in _LOC.finditer(xml):
            entries.append(SitemapEntry(url=m.group(1)))
    return entries


async def fetch_sitemap(http: HttpFetcher, url: str) -> list[SitemapEntry]:
    resp = await http.get(url)
    if resp.status != 200 or not resp.body:
        return []
    return parse_sitemap_xml(resp.body)
