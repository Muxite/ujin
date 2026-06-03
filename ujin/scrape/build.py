"""Shared wiring for the scrape stack — used by both the :8901 app and the
jobs control plane's ``scrape`` source, so the fetch/cache/policy/metrics setup
lives in exactly one place.

:func:`build_scrape_components` constructs the dependency-injected pieces
(HttpFetcher started, ObscuraFetcher, ScrapeCache warmed from disk, HostPolicy,
HostMetrics, overrides) and returns an async ``aclose`` that flushes + closes
them. :func:`build_scrape_service` layers a :class:`ScrapeService` on top with a
given scorer (default :class:`NullScorer`).

The richer :8901 app (:mod:`ujin.scrape.app`) still owns the optional
news-trading bits (BreakingScorer, corroboration, nitter pool, x-trends loop);
those are not needed to drive a generic ``scrape`` job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..cache import HostPolicy, ScrapeCache
from ..cache.disk import DiskCache
from ..fetch import HttpFetcher, ObscuraFetcher
from .config import ScrapeConfig
from .host_overrides import HostOverrideRegistry
from .metrics import HostMetrics
from .scoring import NullScorer, Scorer
from .service import ScrapeService

logger = logging.getLogger("ujin.scrape.build")


@dataclass
class ScrapeComponents:
    """The injected scrape stack (minus the service + optional scorer/trends)."""

    http: HttpFetcher
    obscura: ObscuraFetcher
    cache: ScrapeCache
    policy: HostPolicy
    metrics: HostMetrics
    overrides: HostOverrideRegistry
    disk: Optional[DiskCache]
    browser: Any = None  # BrowserFetcher when cfg.browser_enabled and available


async def build_scrape_components(cfg: ScrapeConfig) -> ScrapeComponents:
    """Build + start the shared fetch/cache/policy/metrics stack."""
    http = HttpFetcher(
        per_host_concurrency=cfg.per_host_concurrency,
        timeout_secs=cfg.http_timeout_secs,
        user_agent=cfg.user_agent,
    )
    await http.start()
    obscura = ObscuraFetcher(timeout_secs=cfg.fetch_timeout_secs)
    cache = ScrapeCache(max_entries=cfg.cache_max_entries, ttl_secs=cfg.cache_ttl_secs)
    policy = HostPolicy(cooldown_secs=cfg.host_cooldown_secs)
    metrics = HostMetrics()
    overrides = (
        HostOverrideRegistry.from_file(cfg.per_host_config_path)
        if cfg.per_host_config_path
        else HostOverrideRegistry()
    )

    disk: DiskCache | None = None
    if cfg.disk_cache_path:
        try:
            disk = DiskCache(cfg.disk_cache_path)
            for key, entry in disk.load_all():
                cache.put(key, entry)
            logger.info("disk cache loaded from %s", cfg.disk_cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("disk cache init failed (%s); continuing without", exc)
            disk = None

    # Optional browser fetcher (Playwright/Selenium) for the `render="browser"`
    # strategy. Built only when enabled and the backend library is present.
    browser = None
    if cfg.browser_enabled:
        from ..fetch.browser import BrowserFetcher, browser_available

        if browser_available(cfg.browser_engine):
            browser = BrowserFetcher(
                engine=cfg.browser_engine, headless=cfg.browser_headless,
                timeout_secs=cfg.browser_timeout_secs, user_agent=cfg.user_agent,
            )
            logger.info("browser fetcher ready (engine=%s)", cfg.browser_engine)
        else:
            logger.warning("browser_enabled but %s not installed; "
                           "browser strategy disabled", cfg.browser_engine)

    return ScrapeComponents(
        http=http, obscura=obscura, cache=cache, policy=policy,
        metrics=metrics, overrides=overrides, disk=disk, browser=browser,
    )


async def close_scrape_components(comps: ScrapeComponents) -> None:
    """Flush the disk cache (if any), close the browser, and close HTTP."""
    if comps.disk is not None:
        try:
            comps.disk.flush_from(list(comps.cache.items()))
            comps.disk.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("disk cache flush failed: %s", exc)
    if comps.browser is not None:
        try:
            await comps.browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("browser close failed: %s", exc)
    await comps.http.close()


async def build_scrape_service(
    cfg: ScrapeConfig,
    *,
    scorer: Optional[Scorer] = None,
) -> tuple[ScrapeService, ScrapeComponents, Callable[[], Awaitable[None]]]:
    """Build a ready :class:`ScrapeService` plus its components and an ``aclose``.

    Used by the jobs control plane to back ``scrape`` sources with the same
    wiring the :8901 service uses.
    """
    comps = await build_scrape_components(cfg)
    service = ScrapeService(
        http=comps.http,
        obscura=comps.obscura,
        cache=comps.cache,
        policy=comps.policy,
        config=cfg,
        metrics=comps.metrics,
        overrides=comps.overrides,
        scorer=scorer or NullScorer(),
        browser=comps.browser,
    )

    async def aclose() -> None:
        await close_scrape_components(comps)

    return service, comps, aclose
