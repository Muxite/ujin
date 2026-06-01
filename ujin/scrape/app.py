"""FastAPI app factory for the scrape service.

``create_scrape_app(config)`` wires the fetch/cache/policy/metrics layer plus a
:class:`ScrapeService` and mounts the route surface. Everything trading-specific
(corroboration store, nitter pool, x-trends loop, BreakingScorer) is optional
and wired by :mod:`ujin.scrape.app` only when the relevant config is present —
the default app is a generic scraper.

This is independent of the poller control service in :mod:`ujin.service`; run
either or both.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from ..cache import HostPolicy, ScrapeCache
from ..cache.disk import DiskCache
from ..fetch import HttpFetcher, ObscuraFetcher
from .config import ScrapeConfig
from .host_overrides import HostOverrideRegistry
from .metrics import HostMetrics
from .routes import router as core_router
from .scoring import NullScorer, Scorer
from .service import ScrapeService

logger = logging.getLogger("ujin.scrape")


def create_scrape_app(
    config: Optional[ScrapeConfig] = None,
    *,
    scorer: Optional[Scorer] = None,
) -> Any:
    """Build the scrape-service FastAPI app.

    ``scorer`` defaults to :class:`NullScorer`. Pass ``ujin.trends.BreakingScorer``
    (with a corroboration store + trend-terms provider) to recover news-trading
    semantics — see :mod:`ujin.trends`.
    """
    try:
        from fastapi import FastAPI
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "ujin scrape service needs the 'scrape' extra: pip install 'ujin[scrape]'"
        ) from exc

    cfg = config or ScrapeConfig.from_env()
    the_scorer = scorer or NullScorer()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = HttpFetcher(
            per_host_concurrency=cfg.per_host_concurrency,
            timeout_secs=cfg.http_timeout_secs,
            user_agent=cfg.user_agent,
        )
        await http.start()
        obscura = ObscuraFetcher(timeout_secs=cfg.fetch_timeout_secs)
        cache = ScrapeCache(
            max_entries=cfg.cache_max_entries, ttl_secs=cfg.cache_ttl_secs
        )
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

        service = ScrapeService(
            http=http,
            obscura=obscura,
            cache=cache,
            policy=policy,
            config=cfg,
            metrics=metrics,
            overrides=overrides,
            scorer=the_scorer,
        )

        app.state.config = cfg
        app.state.http = http
        app.state.obscura = obscura
        app.state.cache = cache
        app.state.policy = policy
        app.state.metrics = metrics
        app.state.overrides = overrides
        app.state.service = service
        app.state.disk_cache = disk
        # M4 hooks — populated when social/trends are wired.
        app.state.nitter_pool = None
        app.state.corroboration = None

        logger.info("ujin scrape service ready")
        try:
            yield
        finally:
            if disk is not None:
                try:
                    disk.flush_from(list(cache.items()))
                    disk.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("disk cache flush failed: %s", exc)
            await http.close()
            logger.info("ujin scrape service stopped")

    app = FastAPI(title="ujin-scrape", version="0.3.0", lifespan=lifespan)
    app.include_router(core_router)
    return app


def serve(
    host: str = "0.0.0.0",
    port: int = 8901,
    config: Optional[ScrapeConfig] = None,
) -> None:
    import uvicorn

    uvicorn.run(create_scrape_app(config), host=host, port=port)
