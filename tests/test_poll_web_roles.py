"""HttpPollable / ApiPollable / RssPollable / ScrapePollable against the fake
origin and duck-typed services. These were at 0% coverage."""
from __future__ import annotations

import pytest

from ujin.fetch.http import HttpFetcher
from ujin.poll.api import ApiPollable, _dig
from ujin.poll.http import HttpPollable
from ujin.poll.rss import RssPollable
from ujin.poll.scrape import ScrapePollable


# ── HttpPollable ─────────────────────────────────────────────────────────────

async def test_http_pollable_change_cycle(fake_origin):
    route = fake_origin.add("/page", body="version one")
    async with HttpFetcher() as http:
        p = HttpPollable(fake_origin.url("/page"), fetcher=http)
        first = await p.poll(None)
        assert first.ok and first.changed
        second = await p.poll(first)
        assert second.ok and not second.changed
        route.body = "version two"
        third = await p.poll(second)
        assert third.changed
        assert third.fingerprint != first.fingerprint


async def test_http_pollable_304_keeps_fingerprint(fake_origin):
    fake_origin.add("/page", body="stable", etag='"v1"')
    async with HttpFetcher() as http:
        p = HttpPollable(fake_origin.url("/page"), fetcher=http)
        first = await p.poll(None)
        second = await p.poll(first)  # sends If-None-Match -> 304
        assert second.status == 304
        assert second.ok and not second.changed
        assert second.fingerprint == first.fingerprint
        assert second.payload["not_modified"] is True


async def test_http_pollable_5xx_not_ok(fake_origin):
    fake_origin.add("/down", body="x", status=503)
    async with HttpFetcher() as http:
        p = HttpPollable(fake_origin.url("/down"), fetcher=http)
        r = await p.poll(None)
        assert r.ok is False and r.status == 503


async def test_http_pollable_connection_error_is_failure():
    p = HttpPollable("http://127.0.0.1:1/")
    r = await p.poll(None)
    assert r.ok is False and r.error
    if p._fetcher is not None:
        await p._fetcher.close()


async def test_http_pollable_render_uses_obscura(obscura_stub_bin):
    p = HttpPollable("https://x.test/spa", render=True)
    r = await p.poll(None)
    assert r.ok is True
    # ObscuraResult flows through fingerprinting like a body
    assert r.fingerprint


# ── ApiPollable ──────────────────────────────────────────────────────────────

async def test_api_pollable_json_path_slices(fake_origin):
    fake_origin.add("/api", body='{"meta": {"ts": 1}, "data": {"items": [1, 2]}}',
                    content_type="application/json")
    p = ApiPollable(fake_origin.url("/api"), json_path="data.items")
    r = await p.poll(None)
    assert r.ok and r.changed
    assert r.payload == [1, 2]  # payload is the selected slice itself


async def test_api_pollable_ignores_unselected_fields(fake_origin):
    route = fake_origin.add("/api", body='{"ts": 1, "items": [1]}',
                            content_type="application/json")
    p = ApiPollable(fake_origin.url("/api"), json_path="items")
    first = await p.poll(None)
    route.body = '{"ts": 999, "items": [1]}'  # only the timestamp moved
    second = await p.poll(first)
    assert second.changed is False


async def test_api_pollable_429_retry_after(fake_origin):
    fake_origin.add("/api", body="slow down", status=429,
                    headers={"Retry-After": "120"})
    p = ApiPollable(fake_origin.url("/api"))
    r = await p.poll(None)
    assert r.ok is False
    assert r.retry_after == 120.0


async def test_api_pollable_post_with_body(fake_origin):
    fake_origin.add("/api", body='{"ok": true}', content_type="application/json")
    p = ApiPollable(fake_origin.url("/api"), method="POST",
                    json_body={"q": "test"}, headers={"X-Auth": "k"})
    r = await p.poll(None)
    assert r.ok
    req = fake_origin.requests[-1]
    assert req.method == "POST"
    assert req.headers["X-Auth"] == "k"


def test_dig_paths():
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert _dig(data, "a.b.1.c") == 2
    assert _dig(data, "a.b.9") is None
    assert _dig(data, "a.missing") is None
    assert _dig(data, None) == data
    assert _dig([1, 2], "x") is None


# ── RssPollable ──────────────────────────────────────────────────────────────

async def test_rss_pollable_tracks_new_urls(fake_origin, monkeypatch):
    feed_path = "tests/fixtures/feeds/feed.xml"
    body = open(feed_path).read()
    fake_origin.add("/feed.xml", body=body, content_type="application/rss+xml")

    p = RssPollable(fake_origin.url("/feed.xml"))
    first = await p.poll(None)
    assert first.ok and first.changed
    assert len(first.payload["urls"]) == 3
    assert first.payload["new_urls"] == first.payload["urls"]

    second = await p.poll(first)
    assert second.changed is False
    assert second.payload["new_urls"] == []


async def test_rss_pollable_unreachable_feed_is_empty_not_error():
    """feedparser swallows transport errors and yields zero entries, so an
    unreachable feed currently reads as ok+empty (not a failure). Documented
    here so a future fix is a conscious contract change."""
    p = RssPollable("http://127.0.0.1:1/feed.xml")
    r = await p.poll(None)
    assert r.ok is True
    assert r.payload["urls"] == []


# ── ScrapePollable ───────────────────────────────────────────────────────────

class _Svc:
    def __init__(self, fp="fp1", hint=None, raises=None):
        self.fp = fp
        self.hint = hint
        self.raises = raises
        self.calls = []

    async def scrape(self, url, *, mode="links", force_refresh=False):
        self.calls.append((url, mode, force_refresh))
        if self.raises:
            raise self.raises

        class _R:
            fingerprint = self.fp
            next_poll_hint_secs = self.hint
        return _R()


async def test_scrape_pollable_change_detection():
    svc = _Svc(fp="aaa", hint=300.0)
    p = ScrapePollable(svc, "https://x.test/", mode="links")
    first = await p.poll(None)
    assert first.ok and first.changed
    assert first.retry_after == 300.0
    second = await p.poll(first)
    assert second.changed is False
    assert svc.calls[0] == ("https://x.test/", "links", False)


async def test_scrape_pollable_error_is_failure():
    svc = _Svc(raises=RuntimeError("fetch failed"))
    p = ScrapePollable(svc, "https://x.test/")
    r = await p.poll(None)
    assert r.ok is False and "fetch failed" in r.error


def test_scrape_pollable_default_key():
    p = ScrapePollable(_Svc(), "https://x.test/", mode="article")
    assert p.key == "scrape:article:https://x.test/"
