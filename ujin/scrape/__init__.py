"""ujin scrape — the rich scrape orchestrator and HTTP surface.

This subpackage layers a full scraping service on top of ujin's fetch/extract/
cache/sources toolkit: an HTTP -> obscura -> altpath fallback chain, per-host
cooldown, fingerprinted change detection, and a pluggable scorer for ranking
links. It reaches feature/endpoint parity with jennie's scraper-v2 so that
service can migrate onto ujin.

The poller (``ujin.engine`` / ``ujin.service``) and this scrape service are
independent: import what you need. Heavy deps (aiohttp/selectolax/trafilatura/
feedparser/fastapi) are pulled by the ``scrape`` extra and imported lazily.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .config import ScrapeConfig

if TYPE_CHECKING:  # pragma: no cover
    from .build import build_scrape_service
    from .service import ScrapeResult, ScrapeService

# The stable import surface (everything else under ujin.scrape is internal).
__all__ = ["ScrapeConfig", "ScrapeService", "ScrapeResult", "build_scrape_service"]


def __getattr__(name: str):
    # Lazy: ScrapeService pulls the web stack (aiohttp/selectolax/trafilatura);
    # importing ujin.scrape for just the config must stay dependency-free.
    if name in ("ScrapeService", "ScrapeResult"):
        from . import service

        return getattr(service, name)
    if name == "build_scrape_service":
        from .build import build_scrape_service

        return build_scrape_service
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
