"""Wiring for the opt-in :class:`BreakingScorer` (news-trading semantics).

:func:`wire_breaking_scorer` builds everything a working `BreakingScorer` needs
from a :class:`ujin.scrape.config.ScrapeConfig` — a
:class:`CorroborationStore`, a background x-trends refresh loop, and
:class:`Weights` — and packages it all into a :class:`BreakingScorerWiring`
the caller can fold into its own ``app.state``/teardown with minimal code.

This was extracted out of :mod:`ujin.scrape.app` (the generic scrape-app
factory should not carry news-trading wiring inline — see that module's
docstring) as a pure refactor: :func:`wire_breaking_scorer` does exactly what
``create_scrape_app`` used to do inline when ``cfg.enable_breaking_scorer`` was
set, nothing more.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..scrape.config import ScrapeConfig
from ..scrape.host_overrides import HostOverrideRegistry
from .corroboration import CorroborationStore
from .scorer import BreakingScorer
from .tier import Weights

logger = logging.getLogger("ujin.trends.wiring")


@dataclass
class BreakingScorerWiring:
    """Everything :func:`wire_breaking_scorer` built, packaged for the caller.

    ``scorer`` is the ready :class:`BreakingScorer`; ``corroboration`` is its
    backing store, exposed separately so a caller can surface it on its own
    state (e.g. ``app.state.corroboration``, the way :mod:`ujin.scrape.app`
    did before this was extracted); ``refresh_task`` is the background asyncio
    task that keeps ``scorer``'s trend-terms provider warm. Call
    :meth:`aclose` on teardown to cancel and await it cleanly.
    """

    scorer: BreakingScorer
    corroboration: CorroborationStore
    refresh_task: "asyncio.Task"

    async def aclose(self) -> None:
        """Cancel the x-trends refresh loop and await its completion.

        Mirrors the inline teardown ``create_scrape_app`` used to run: cancel,
        then await, swallowing both a clean ``CancelledError`` and anything
        else the task might raise (it never should, but teardown must not
        blow up on it).
        """
        self.refresh_task.cancel()
        try:
            await self.refresh_task
        except (asyncio.CancelledError, Exception):
            pass


def wire_breaking_scorer(
    cfg: ScrapeConfig, overrides: Optional[HostOverrideRegistry] = None
) -> BreakingScorerWiring:
    """Build a :class:`BreakingScorer` + its corroboration store + trends loop.

    Reproduces, byte-for-byte, what ``create_scrape_app`` used to wire inline
    when ``cfg.enable_breaking_scorer`` was set: a :class:`CorroborationStore`
    sized from ``cfg.corroboration_*``/``cfg.headline_ring_max``, a background
    task polling :func:`ujin.sources.social.fetch_x_trends` (US region, top 30)
    every 5 minutes, and a :class:`BreakingScorer` wired to both plus
    :class:`Weights` from ``cfg.tier_weight_*`` / ``cfg.breaking_threshold``.

    *overrides* is the same :class:`HostOverrideRegistry` the generic scrape
    stack already built (``comps.overrides``) — pass it through so per-host
    tier overrides keep applying under ``BreakingScorer`` exactly as before.
    """
    from ..sources.social import fetch_x_trends

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

    refresh_task = asyncio.create_task(_refresh_trends_loop())
    scorer = BreakingScorer(
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
    return BreakingScorerWiring(
        scorer=scorer, corroboration=corroboration, refresh_task=refresh_task,
    )
