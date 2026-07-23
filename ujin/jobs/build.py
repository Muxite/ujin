"""Shared wiring for the job-tracking stack — used by both the jobs control
plane (:mod:`ujin.jobs.app`, the :8902 FastAPI service) and any headless
in-process consumer that just wants a working job-tracking stack without a
FastAPI/WS layer, so the store/engine/scrape-backend setup lives in exactly
one place.

:func:`build_job_manager` wires a :class:`ujin.jobs.store.JobStore` at a given
path, a fresh :class:`ujin.engine.PollEngine`, and (best-effort) a shared
scrape stack (:mod:`ujin.scrape.build`) to back ``scrape``-kind sources, then
returns a ready :class:`ujin.jobs.manager.JobManager` around them plus an
``aclose``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from ujin.engine import PollEngine

from .manager import JobManager
from .store import JobStore

if TYPE_CHECKING:
    from ujin.scrape.service import ScrapeService

logger = logging.getLogger("ujin.jobs.build")


async def build_job_manager(
    db_path: str | Path,
    *,
    scrape_config: Any = None,
    hub: Any = None,
) -> tuple[JobManager, JobStore, Optional["ScrapeService"], Callable[[], Awaitable[None]]]:
    """Build a ready :class:`JobManager` (+ its store, + its optional scrape
    service) plus an ``aclose``.

    Used by both the jobs control plane (:mod:`ujin.jobs.app`) and any
    headless in-process consumer that just wants a working job-tracking
    stack without a FastAPI/WS layer (awork's discovery agent is one).

    Wires a :class:`JobStore` at *db_path*, a fresh :class:`PollEngine`, and
    (best-effort) a shared scrape stack for ``scrape``-kind sources — a
    missing scrape extra/network stack degrades to ``scrape_service=None``
    (that source kind just becomes unavailable), never raises. Does NOT call
    ``manager.load_from_store()`` — call it yourself if you want persisted
    jobs to resume on construction.

    *scrape_config* defaults to a zero-args ``ScrapeConfig()`` when left
    ``None``. This factory never builds a ``ScrapeConfig.from_env()`` itself —
    that is an app.py-specific choice (it couples to that app's own env-var
    conventions); pass your own pre-built config if you want env sourcing.

    *hub* is an optional passthrough (e.g. a WS broadcast hub) forwarded
    straight to ``JobManager(..., hub=hub, ...)``. Constructing a hub is the
    caller's own concern — the FastAPI app owns its ``_Hub()``, this generic
    factory does not.
    """
    store = JobStore(db_path)
    engine = PollEngine()

    # Shared scrape stack for `scrape` sources (optional — degrade if extras
    # or network stack are unavailable).
    scrape_service = None
    scrape_close = None
    try:
        from ujin.scrape.build import build_scrape_service
        from ujin.scrape.config import ScrapeConfig

        cfg = scrape_config if scrape_config is not None else ScrapeConfig()
        scrape_service, _comps, scrape_close = await build_scrape_service(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape source backend unavailable: %s", exc)

    manager = JobManager(engine, store, hub=hub, scrape_service=scrape_service)

    async def aclose() -> None:
        """Close the scrape stack (if built) and the store, each best-effort."""
        if scrape_close is not None:
            try:
                await scrape_close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("scrape stack close failed: %s", exc)
        try:
            store.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("job store close failed: %s", exc)

    return manager, store, scrape_service, aclose
