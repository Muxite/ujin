"""ujin.jobs.build.build_job_manager: the shared JobManager/JobStore/scrape-
stack wiring factory used by both the jobs control plane and any headless
in-process consumer (e.g. awork's discovery agent, backend/awork/apply/
ujin_seam.py).
"""
from __future__ import annotations

import pytest

from ujin.jobs.build import build_job_manager
from ujin.jobs.manager import JobManager
from ujin.jobs.store import JobStore


async def test_build_job_manager_happy_path(tmp_path):
    manager, store, scrape_service, aclose = await build_job_manager(
        tmp_path / "jobs.db"
    )
    try:
        assert isinstance(manager, JobManager)
        assert isinstance(store, JobStore)
        assert manager.store is store
        assert scrape_service is not None
        assert manager.scrape_service is scrape_service
    finally:
        await aclose()


async def test_build_job_manager_scrape_unavailable_degrades(tmp_path, monkeypatch):
    import ujin.scrape.build as _sb

    async def _fail(cfg):
        raise RuntimeError("forced failure for test")

    monkeypatch.setattr(_sb, "build_scrape_service", _fail)

    manager, store, scrape_service, aclose = await build_job_manager(
        tmp_path / "jobs.db"
    )
    try:
        assert scrape_service is None
        assert manager.scrape_service is None
        assert isinstance(manager, JobManager)
    finally:
        await aclose()  # must not raise even though the scrape stack never built


async def test_build_job_manager_hub_passthrough(tmp_path):
    sentinel = object()
    manager, store, _scrape_service, aclose = await build_job_manager(
        tmp_path / "jobs.db", hub=sentinel
    )
    try:
        assert manager.hub is sentinel
    finally:
        await aclose()
