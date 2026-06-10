"""HttpFetcher against a real local aiohttp origin (no internet).

Covers status handling, conditional GET (ETag → 304), redirects, encoding
fallback, per-host concurrency, and session lifecycle.
"""
from __future__ import annotations

import asyncio

import pytest

from ujin.fetch.http import HttpFetcher



async def test_get_200_captures_body_and_validators(fake_origin):
    fake_origin.add("/page", body="<html><body>hello</body></html>",
                    etag='"v1"', headers={"Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT"})
    async with HttpFetcher() as http:
        resp = await http.get(fake_origin.url("/page"))
    assert resp.status == 200
    assert "hello" in resp.body
    assert resp.etag == '"v1"'
    assert resp.last_modified == "Mon, 01 Jan 2026 00:00:00 GMT"
    assert resp.not_modified is False
    assert resp.final_url.endswith("/page")
    assert resp.elapsed_ms >= 0


async def test_conditional_get_304(fake_origin):
    fake_origin.add("/page", body="x", etag='"v1"')
    async with HttpFetcher() as http:
        first = await http.get(fake_origin.url("/page"))
        second = await http.get(fake_origin.url("/page"), etag=first.etag)
    assert second.status == 304
    assert second.not_modified is True
    assert second.body == ""


async def test_if_modified_since_header_sent(fake_origin):
    fake_origin.add("/page", body="x")
    async with HttpFetcher() as http:
        await http.get(fake_origin.url("/page"),
                       last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    sent = fake_origin.requests[-1].headers
    assert sent["If-Modified-Since"] == "Mon, 01 Jan 2026 00:00:00 GMT"


async def test_extra_headers_merged(fake_origin):
    fake_origin.add("/page", body="x")
    async with HttpFetcher() as http:
        await http.get(fake_origin.url("/page"),
                       extra_headers={"X-Probe": "1"})
    assert fake_origin.requests[-1].headers["X-Probe"] == "1"


async def test_error_statuses_pass_through(fake_origin):
    fake_origin.add("/forbidden", body="no", status=403)
    fake_origin.add("/broken", body="boom", status=500)
    async with HttpFetcher() as http:
        r403 = await http.get(fake_origin.url("/forbidden"))
        r500 = await http.get(fake_origin.url("/broken"))
    assert r403.status == 403 and r403.body == "no"
    assert r500.status == 500 and r500.body == "boom"


async def test_404_for_unknown_path(fake_origin):
    async with HttpFetcher() as http:
        resp = await http.get(fake_origin.url("/nowhere"))
    assert resp.status == 404


async def test_redirect_followed_final_url(fake_origin):
    fake_origin.add("/old", status=302,
                    headers={"Location": fake_origin.url("/new")})
    fake_origin.add("/new", body="landed")
    async with HttpFetcher() as http:
        resp = await http.get(fake_origin.url("/old"))
    assert resp.status == 200
    assert resp.body == "landed"
    assert resp.final_url.endswith("/new")


async def test_invalid_utf8_replaced_not_raised(fake_origin):
    fake_origin.add("/binary", body=b"ok \xff\xfe broken")
    async with HttpFetcher() as http:
        resp = await http.get(fake_origin.url("/binary"))
    assert resp.status == 200
    assert "ok" in resp.body  # errors="replace" never raises


async def test_timeout_raises(fake_origin):
    fake_origin.add("/slow", body="late", delay=5.0)
    # Sub-second total timeout: aiohttp surfaces TimeoutError.
    async with HttpFetcher(timeout_secs=1) as http:
        http._timeout = __import__("aiohttp").ClientTimeout(total=0.2)
        http._session = None  # force re-open with the tighter timeout
        with pytest.raises(asyncio.TimeoutError):
            await http.get(fake_origin.url("/slow"))


async def test_per_host_concurrency_enforced(fake_origin):
    """With per_host_concurrency=1, parallel GETs to one host serialize."""
    fake_origin.add("/a", body="a", delay=0.05)
    fake_origin.add("/b", body="b", delay=0.05)
    async with HttpFetcher(per_host_concurrency=1) as http:
        await asyncio.gather(
            http.get(fake_origin.url("/a")),
            http.get(fake_origin.url("/b")),
        )
    assert fake_origin.max_inflight == 1


async def test_per_host_semaphore_allows_parallelism_when_higher(fake_origin):
    fake_origin.add("/a", body="a", delay=0.05)
    fake_origin.add("/b", body="b", delay=0.05)
    async with HttpFetcher(per_host_concurrency=4) as http:
        await asyncio.gather(
            http.get(fake_origin.url("/a")),
            http.get(fake_origin.url("/b")),
        )
    assert fake_origin.max_inflight == 2


async def test_host_sem_created_once_per_host(fake_origin):
    fake_origin.add("/x", body="x")
    async with HttpFetcher() as http:
        await asyncio.gather(*(http.get(fake_origin.url("/x")) for _ in range(8)))
        assert len(http._host_locks) == 1


async def test_lazy_start_on_first_get(fake_origin):
    fake_origin.add("/x", body="x")
    http = HttpFetcher()
    assert http._session is None
    try:
        resp = await http.get(fake_origin.url("/x"))  # opens session implicitly
        assert resp.status == 200
        assert http._session is not None
    finally:
        await http.close()
    assert http._session is None


async def test_close_idempotent():
    http = HttpFetcher()
    await http.close()
    await http.close()  # double close must not raise


async def test_user_agent_header_sent(fake_origin):
    fake_origin.add("/x", body="x")
    async with HttpFetcher(user_agent="ujin-test/1.0") as http:
        await http.get(fake_origin.url("/x"))
    assert fake_origin.requests[-1].headers["User-Agent"] == "ujin-test/1.0"
