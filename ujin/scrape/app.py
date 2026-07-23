"""FastAPI app factory for the scrape service.

``create_scrape_app(config)`` wires the fetch/cache/policy/metrics layer plus a
:class:`ScrapeService` and mounts the route surface — this is the generic,
low-level scraper. News-trading semantics (corroboration store, x-trends loop,
``BreakingScorer``) are opt-in and built by :mod:`ujin.trends.wiring`, not
inline here; this module just folds that wiring's pieces into its own
``app.state``/teardown when ``cfg.enable_breaking_scorer`` is set.

For anything else, this app never has to know a scorer's implementation:
``cfg.scorer_kind`` resolves any `scorer` kind registered with
:data:`ujin.registry.register`, including one an operator drops into
``UJIN_PLUGINS_DIR`` as foreign code (see :mod:`ujin.plugins`) — the same
mechanism :mod:`ujin.jobs.app` already uses for sources/transforms/sinks.

This is independent of the poller control service in :mod:`ujin.service`; run
either or both.
"""

from __future__ import annotations

import logging
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
    semantics — see :mod:`ujin.trends`. Setting ``cfg.enable_breaking_scorer``
    has this factory wire that up for you (see :mod:`ujin.trends.wiring`).

    For any other scorer, set ``cfg.scorer_kind`` to a kind registered with
    :data:`ujin.registry.register` — a built-in, or (more likely) one an
    operator dropped into ``UJIN_PLUGINS_DIR`` as foreign code with zero edits
    to this tree; see :mod:`ujin.plugins`. Plugins are loaded before the
    scorer is resolved, so a plugin may supply the very kind ``scorer_kind``
    names.
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

        # Load operator plugins BEFORE resolving the scorer below, so a
        # mounted plugin registering `cfg.scorer_kind` is available in time
        # (mirrors ujin.jobs.app's "plugins before anything that might
        # reference one" ordering).
        from ujin.plugins import load_plugins

        plugin_status = load_plugins()
        app.state.plugins = plugin_status

        # Optional nitter pool for the X chain's free leg. This is independent
        # of the scorer resolved below — routes_social.py's /social endpoints
        # use it regardless of whether a BreakingScorer is wired.
        nitter_pool = None
        if cfg.nitter_pool_path:
            from ..sources.social import NitterPool

            nitter_pool = NitterPool.from_yaml(cfg.nitter_pool_path)
            logger.info("nitter pool: %d mirrors", len(nitter_pool.mirrors))

        # Resolve the scorer. An explicit `scorer` wins; otherwise wire a
        # BreakingScorer (+ corroboration store + x-trends loop) when enabled;
        # otherwise resolve `scorer_kind` via the plugin/built-in registry
        # (foreign code, no ujin-tree edit needed); else stay generic with
        # NullScorer.
        wiring = None
        scorer_error: Optional[str] = None
        the_scorer: Scorer
        if scorer is not None:
            the_scorer = scorer
        elif cfg.enable_breaking_scorer:
            from ..trends.wiring import wire_breaking_scorer

            wiring = wire_breaking_scorer(cfg, overrides)
            the_scorer = wiring.scorer
        elif cfg.scorer_kind:
            from dataclasses import asdict

            from ..registry import BuildContext, register

            try:
                the_scorer = register.build_scorer(
                    cfg.scorer_kind, asdict(cfg), BuildContext()
                )
            except KeyError as exc:
                # A mistyped/missing scorer_kind is a config error, not a
                # reason to refuse to serve the rest of the app — degrade to
                # NullScorer (same as leaving scorer_kind unset) and report it
                # on /health, mirroring how a bad workflow file or ingest plan
                # degrades in ujin.jobs.app rather than crashing startup.
                scorer_error = str(exc)
                logger.warning(
                    "scorer_kind %r not resolvable (%s); falling back to "
                    "NullScorer", cfg.scorer_kind, exc,
                )
                the_scorer = NullScorer()
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
        app.state.corroboration = wiring.corroboration if wiring is not None else None
        app.state.scorer_error = scorer_error

        logger.info("ujin scrape service ready")
        try:
            yield
        finally:
            if wiring is not None:
                await wiring.aclose()
            await close_scrape_components(comps)
            logger.info("ujin scrape service stopped")

    app = FastAPI(title="ujin-scrape", version="0.4.0", lifespan=lifespan)

    from ..auth import mount_api_key

    mount_api_key(app)
    app.include_router(core_router)
    app.include_router(social_router)

    @app.post("/plugins/reload")
    def plugins_reload() -> dict[str, Any]:
        """Re-scan `UJIN_PLUGINS_DIR`: clear plugin-contributed kinds, re-import.

        Mirrors `ujin.jobs.app`'s endpoint of the same name/shape. An
        already-built scorer/service keeps running unchanged — this only
        refreshes what the registry can resolve for future config/reload.
        """
        from ujin.plugins import load_plugins
        from ujin.registry import register

        register.clear_plugins()
        status = load_plugins()
        app.state.plugins = status
        return status

    return app


def serve(
    host: str = "0.0.0.0",
    port: int = 8901,
    config: Optional[ScrapeConfig] = None,
) -> None:
    import uvicorn

    uvicorn.run(create_scrape_app(config), host=host, port=port)
