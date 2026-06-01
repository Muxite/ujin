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

from .config import ScrapeConfig

__all__ = ["ScrapeConfig"]
