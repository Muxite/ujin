"""Fetch layer: HTTP client, obscura render fallback, alternate-path chain.

These pull the ``web`` extra (aiohttp/selectolax). Import from here for the
public surface; submodules keep their heavy imports lazy where possible.
"""
from __future__ import annotations

from .altpath import AltPathResult, try_rss_fallback, try_sitemap_news
from .http import HttpFetcher, HttpResponse
from .obscura import (
    ObscuraError,
    ObscuraFetcher,
    ObscuraResult,
    ObscuraTimeout,
    obscura_available,
)

__all__ = [
    "HttpFetcher",
    "HttpResponse",
    "ObscuraFetcher",
    "ObscuraResult",
    "ObscuraError",
    "ObscuraTimeout",
    "obscura_available",
    "AltPathResult",
    "try_sitemap_news",
    "try_rss_fallback",
]
