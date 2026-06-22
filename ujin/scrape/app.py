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

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from .build import build_scrape_components, close_scrape_components
from .config import ScrapeConfig
from .routes import router as core_router
from .routes_social import router as social_router
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Shared fetch/cache/policy/metrics stack (also used by the jobs
        # control plane's `scrape` source) — see ujin.scrape.build.
        comps = await build_scrape_components(cfg)
        http = comps.http
        obscura = comps.obscura
        cache = comps.cache
        policy = comps.policy
        metrics = comps.metrics
        overrides = comps.overrides

        # Optional nitter pool for the X chain's free leg.
        nitter_pool = None
        if cfg.nitter_pool_path:
            from ..sources.social import NitterPool

            nitter_pool = NitterPool.from_yaml(cfg.nitter_pool_path)
            logger.info("nitter pool: %d mirrors", len(nitter_pool.mirrors))

        # Resolve the scorer. An explicit `scorer` wins; otherwise wire a
        # BreakingScorer (+ corroboration store + x-trends loop) when enabled,
        # else stay generic with NullScorer.
        corroboration = None
        trends_task: Optional[asyncio.Task] = None
        the_scorer: Scorer
        if scorer is not None:
            the_scorer = scorer
        elif cfg.enable_breaking_scorer:
            from ..sources.social import fetch_x_trends
            from ..trends import BreakingScorer, CorroborationStore, Weights

            corroboration = CorroborationStore(
                window_secs=cfg.corroboration_window_secs,
                max_entries=cfg.headline_ring_max,
                min_hosts_for_corroboration=cfg.corroboration_min_hosts,
                max_hosts_for_full_score=cfg.corroboration_max_hosts_for_full_score,
            )
            trends_state = {"terms": [], "fetched_at": 0.0}

            async def _refresh_trends_loop():
                while True:
                    try:
                        result = await fetch_x_trends("united-states", 30)
                        trends_state["terms"] = [
                            t.tag.lstrip("#") for t in result.items if t.tag
                        ]
                        trends_state["fetched_at"] = time.time()
                        logger.info(
                            "x-trends refreshed: %d terms via %s",
                            len(trends_state["terms"]), result.source,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("x-trends refresh failed: %s", exc)
                    await asyncio.sleep(300.0)

            trends_task = asyncio.create_task(_refresh_trends_loop())
            the_scorer = BreakingScorer(
                overrides=overrides,
                corroboration=corroboration,
                trend_terms_provider=lambda: list(trends_state["terms"]),
                weights=Weights(
                    source_rank=cfg.tier_weight_source_rank,
                    lede_marker=cfg.tier_weight_lede_marker,
                    recency=cfg.tier_weight_recency,
                    corroboration=cfg.tier_weight_corroboration,
                    trend_overlap=cfg.tier_weight_trend_overlap,
                ),
                breaking_threshold=cfg.breaking_threshold,
            )
        else:
            the_scorer = NullScorer()

        disk = comps.disk

        service = ScrapeService(
            http=http,
            obscura=obscura,
            cache=cache,
            policy=policy,
            config=cfg,
            metrics=metrics,
            overrides=overrides,
            scorer=the_scorer,
            browser=comps.browser,
            strategy_feedback=comps.strategy,
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
        app.state.nitter_pool = nitter_pool
        app.state.corroboration = corroboration

        logger.info("ujin scrape service ready")
        try:
            yield
        finally:
            if trends_task is not None:
                trends_task.cancel()
                try:
                    await trends_task
                except (asyncio.CancelledError, Exception):
                    pass
            await close_scrape_components(comps)
            logger.info("ujin scrape service stopped")

    app = FastAPI(title="ujin-scrape", version="0.4.0", lifespan=lifespan)

    from ..auth import mount_api_key

    mount_api_key(app)
    app.include_router(core_router)
    app.include_router(social_router)
    return app


def serve(
    host: str = "0.0.0.0",
    port: int = 8901,
    config: Optional[ScrapeConfig] = None,
) -> None:
    import uvicorn

    uvicorn.run(create_scrape_app(config), host=host, port=port)
