"""Coverage gap-fill tests.

Covers three previously uncovered areas:
- ujin/poll/__init__.py  lazy __getattr__ (HttpPollable / RssPollable /
  ApiPollable + AttributeError path)
- ujin/sources/social/_nitter.py  nitter_posts() async logic
- ujin/sources/social/_syndication.py  syndication_posts() async logic

All tests are offline — no real network, DNS, or aiohttp connections.
"""
from __future__ import annotations

import asyncio
import pytest

# ── ujin/poll/__init__.py ────────────────────────────────────────────────────


def test_poll_lazy_http_pollable():
    import ujin.poll as p

    cls = p.HttpPollable
    assert cls.__name__ == "HttpPollable"


def test_poll_lazy_rss_pollable():
    import ujin.poll as p

    cls = p.RssPollable
    assert cls.__name__ == "RssPollable"


def test_poll_lazy_api_pollable():
    import ujin.poll as p

    cls = p.ApiPollable
    assert cls.__name__ == "ApiPollable"


def test_poll_unknown_attr_raises():
    import ujin.poll as p

    with pytest.raises(AttributeError, match="no attribute"):
        _ = p.NonExistentPollable


# ── ujin/sources/social/_nitter.py ───────────────────────────────────────────


from ujin.sources.social._nitter import NitterPool, nitter_posts  # noqa: E402
from ujin.sources.rss import FeedItem  # noqa: E402


async def test_nitter_posts_empty_username():
    """Blank / @ username short-circuits before any pool access."""
    pool = NitterPool.from_list(["https://n.test"])
    posts = await nitter_posts(pool, "@", count=5)
    assert posts == []


async def test_nitter_posts_no_healthy_mirrors():
    """Empty pool returns [] immediately."""
    pool = NitterPool.from_list([])
    posts = await nitter_posts(pool, "user")
    assert posts == []


async def test_nitter_posts_success(monkeypatch):
    """A good parse_feed response records success and returns posts."""

    async def _feed(url, *, timeout_secs=10):
        return [
            FeedItem(url="https://x.com/u/status/1", title="first", summary=""),
            FeedItem(url="https://x.com/u/status/2", title="", summary="second"),
            FeedItem(url="", title="no url so dropped", summary=""),
        ]

    monkeypatch.setattr("ujin.sources.social._nitter.parse_feed", _feed)
    pool = NitterPool.from_list(["https://n.test"])
    posts = await nitter_posts(pool, "user", count=10)

    assert len(posts) == 2
    assert posts[0].url == "https://x.com/u/status/1"
    assert posts[0].text == "first"
    assert posts[1].text == "second"
    assert pool.mirrors[0].successes == 1
    assert pool.mirrors[0].failures == 0


async def test_nitter_posts_count_cap(monkeypatch):
    """count parameter limits returned posts."""

    async def _feed(url, *, timeout_secs=10):
        return [FeedItem(url=f"https://x.com/u/{i}", title=f"t{i}", summary="")
                for i in range(10)]

    monkeypatch.setattr("ujin.sources.social._nitter.parse_feed", _feed)
    pool = NitterPool.from_list(["https://n.test"])
    posts = await nitter_posts(pool, "user", count=3)
    assert len(posts) == 3


async def test_nitter_posts_exception_records_failure(monkeypatch):
    """parse_feed exception triggers record_failure and falls through."""

    async def _fail(url, *, timeout_secs=10):
        raise ConnectionError("mirror down")

    monkeypatch.setattr("ujin.sources.social._nitter.parse_feed", _fail)
    pool = NitterPool.from_list(["https://n.test"])
    posts = await nitter_posts(pool, "user")
    assert posts == []
    assert pool.mirrors[0].failures == 1


async def test_nitter_posts_empty_items_records_failure(monkeypatch):
    """Empty item list counts as failure so the mirror score drops."""

    async def _empty(url, *, timeout_secs=10):
        return []

    monkeypatch.setattr("ujin.sources.social._nitter.parse_feed", _empty)
    pool = NitterPool.from_list(["https://n.test"])
    posts = await nitter_posts(pool, "user")
    assert posts == []
    assert pool.mirrors[0].failures == 1


async def test_nitter_posts_falls_to_second_mirror(monkeypatch):
    """First mirror fails; second mirror succeeds."""
    calls: list[str] = []

    async def _feed(url, *, timeout_secs=10):
        calls.append(url)
        if "bad" in url:
            raise RuntimeError("bad mirror")
        return [FeedItem(url="https://x.com/u/1", title="ok", summary="")]

    monkeypatch.setattr("ujin.sources.social._nitter.parse_feed", _feed)
    pool = NitterPool.from_list(["https://bad.test", "https://good.test"])
    posts = await nitter_posts(pool, "user")
    assert len(posts) == 1
    assert pool.mirrors[0].failures == 1
    assert pool.mirrors[1].successes == 1


# ── ujin/sources/social/_syndication.py ─────────────────────────────────────


from ujin.sources.social._syndication import syndication_posts  # noqa: E402
from ujin.sources.social.twitter import SocialPost  # noqa: E402


class _SyndResp:
    """Async context-manager response stub for syndication tests."""

    def __init__(self, status: int, body, content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"content-type": content_type}

    async def json(self, *, content_type=None):
        return self._body

    async def text(self):
        if isinstance(self._body, str):
            return self._body
        import json
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _SyndSession:
    """Minimal async-context-manager aiohttp.ClientSession stub."""

    def __init__(self, resp: _SyndResp):
        self._resp = resp
        self.closed = False

    def get(self, *a, **kw):
        return self._resp

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


_SYND_JSON_PAYLOAD = {
    "timeline": {"entries": [
        {"type": "tweet", "content": {"tweet": {
            "full_text": "syndication works",
            "permalink_url": "https://x.com/u/status/1",
        }}},
    ]}
}


async def test_syndication_empty_username():
    """Empty / @ username returns [] immediately."""
    posts = await syndication_posts("@", count=5)
    assert posts == []


async def test_syndication_non200_returns_empty():
    """Non-200 HTTP status returns []."""
    sess = _SyndSession(_SyndResp(429, "rate limited", "text/plain"))
    posts = await syndication_posts("user", count=5, session=sess)
    assert posts == []


async def test_syndication_json_content_type(monkeypatch):
    """JSON content-type response is parsed via _from_json."""
    sess = _SyndSession(_SyndResp(200, _SYND_JSON_PAYLOAD, "application/json"))
    posts = await syndication_posts("user", count=10, session=sess)
    assert len(posts) == 1
    assert posts[0].url == "https://x.com/u/status/1"
    assert posts[0].text == "syndication works"


async def test_syndication_text_json_fallback():
    """text/html response containing valid JSON is still parsed via _from_json."""
    import json
    body_text = json.dumps(_SYND_JSON_PAYLOAD)
    sess = _SyndSession(_SyndResp(200, body_text, "text/html"))
    posts = await syndication_posts("user", count=10, session=sess)
    assert len(posts) == 1


async def test_syndication_html_body_response():
    """text/html response with HTML (not JSON) falls back to _from_html."""
    html = (
        '<div class="timeline-Tweet">'
        '<p class="timeline-Tweet-text">html fallback text</p>'
        '<a href="https://twitter.com/u/status/9">link</a>'
        "</div>"
    )
    sess = _SyndSession(_SyndResp(200, html, "text/html"))
    posts = await syndication_posts("user", count=5, session=sess)
    assert len(posts) == 1
    assert posts[0].url == "https://twitter.com/u/status/9"


async def test_syndication_client_error_returns_empty():
    """aiohttp.ClientError is caught and returns []."""
    import aiohttp

    class _ErrorResp:
        status = 200
        headers = {"content-type": "application/json"}

        def get(self, *a, **kw):
            return self

        async def __aenter__(self):
            raise aiohttp.ClientConnectionError("network down")

        async def __aexit__(self, *_):
            pass

    class _ErrorSession:
        def get(self, *a, **kw):
            return _ErrorResp()

        async def close(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    posts = await syndication_posts("user", count=5, session=_ErrorSession())
    assert posts == []


async def test_syndication_timeout_returns_empty():
    """asyncio.TimeoutError is caught and returns []."""

    class _TimeoutResp:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *_):
            pass

    class _TimeoutSession:
        def get(self, *a, **kw):
            return _TimeoutResp()

        async def close(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    posts = await syndication_posts("user", count=5, session=_TimeoutSession())
    assert posts == []


async def test_syndication_own_session_created(monkeypatch):
    """When no session is injected, the function creates and closes its own."""
    created: list = []
    closed: list = []

    class _FakeClientSession:
        def __init__(self, *a, **kw):
            created.append(self)

        def get(self, *a, **kw):
            return _SyndResp(200, _SYND_JSON_PAYLOAD, "application/json")

        async def close(self):
            closed.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            await self.close()

    monkeypatch.setattr("aiohttp.ClientSession", _FakeClientSession)
    posts = await syndication_posts("user", count=10)
    assert len(posts) == 1
    assert len(created) == 1 and len(closed) == 1
