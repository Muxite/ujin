"""Async hot paths via the custom runner in _aio.py.

Each test measures, prints, checks against benchmarks/baseline.json (4x
tolerance), and — with UJIN_BENCH_RECORD=1 — rewrites the baseline.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from ujin.adapt.concurrency import TokenBucket
from ujin.cache import CachedEntry, HostPolicy, ScrapeCache
from ujin.cache.disk import DiskCache
from ujin.engine import PollEngine
from ujin.poll.base import PollResult

from _aio import abench, check_against_baseline, record


class _NoopPollable:
    __slots__ = ("key",)

    def __init__(self, key: str):
        self.key = key

    async def poll(self, prev):
        return PollResult(ok=True, changed=False, fingerprint="fp")


async def test_engine_sweep_1k_targets():
    engine = PollEngine(max_concurrency=32,
                        token_bucket=TokenBucket(rate=1e9, burst=1e9))
    for i in range(1000):
        engine.add(_NoopPollable(f"t{i}"), base=60, jitter="none")

    r = await abench("engine_sweep_1k", engine.sweep, iterations=10)
    record([r])
    check_against_baseline(r)


async def test_scrape_cache_hit_path():
    """ScrapeService serving a host-cooldown cache hit — the cheapest
    full-service path (no fetch)."""
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    class _NeverHttp:
        async def get(self, url, **kw):  # pragma: no cover - cache must win
            raise AssertionError("cache-hit path must not fetch")

    url = "https://bench.example.com/"
    cache = ScrapeCache()
    cache.put(f"links:{url}", CachedEntry(
        url=url, fingerprint="fp", payload={"links": []},
        fetched_at=time.monotonic()))
    policy = HostPolicy(cooldown_secs=3600)
    policy.record_failure(url)  # forces the cooldown -> cache branch
    svc = ScrapeService(http=_NeverHttp(), obscura=None, cache=cache,
                        policy=policy, config=ScrapeConfig())

    async def hit():
        await svc.scrape(url, mode="links")

    r = await abench("scrape_cache_hit", hit, iterations=200)
    record([r])
    check_against_baseline(r)


async def test_disk_cache_roundtrip(tmp_path):
    db = DiskCache(tmp_path / "bench.db")
    entry = CachedEntry(url="u", fingerprint="f",
                        payload={"links": [{"u": i} for i in range(100)]},
                        fetched_at=time.monotonic())

    async def roundtrip():
        await asyncio.to_thread(db.put, "k", entry)
        return await asyncio.to_thread(db.get, "k")

    r = await abench("disk_cache_roundtrip", roundtrip, iterations=50)
    db.close()
    record([r])
    check_against_baseline(r)


async def test_http_fetch_throughput_local():
    """HttpFetcher against a local aiohttp origin: 32 GETs per iteration
    through the per-host semaphore."""
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from ujin.fetch.http import HttpFetcher

    async def handler(request):
        return web.Response(text="<html><body>ok</body></html>",
                            content_type="text/html")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()

    async with HttpFetcher(per_host_concurrency=8) as http:
        url = str(server.make_url("/page"))

        async def burst():
            await asyncio.gather(*(http.get(url) for _ in range(32)))

        r = await abench("http_32_gets_local", burst, iterations=10)
    await server.close()
    record([r])
    check_against_baseline(r)
