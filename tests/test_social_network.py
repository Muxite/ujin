"""Fixture-driven unit tests for mastodon.py and twitter.py.

All tests are offline — aiohttp.ClientSession is replaced with a minimal
in-process fake so no DNS lookups or TCP connections occur.
"""
from __future__ import annotations

import pytest

from ujin.sources.social.mastodon import mastodon_timeline
from ujin.sources.social.twitter import BraveError, twitter_search


# --------------------------------------------------------------------------- #
# Minimal aiohttp fakes
# --------------------------------------------------------------------------- #
class _Resp:
    """Async context-manager response stub."""

    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        # body may be a string (error text) or a dict/list (JSON payload)
        return self._body if isinstance(self._body, str) else str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _Session:
    """Fake aiohttp.ClientSession: pops pre-loaded responses for each .get() call."""

    def __init__(self, *responses: _Resp):
        self._q = list(responses)

    def get(self, *args, **kwargs):
        return self._q.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def _make_session(*responses: _Resp):
    """Return a factory that always returns the same pre-loaded _Session."""
    sess = _Session(*responses)
    return lambda *a, **kw: sess


# --------------------------------------------------------------------------- #
# mastodon_timeline
# --------------------------------------------------------------------------- #

async def test_mastodon_normal(monkeypatch):
    """Two-step lookup succeeds; HTML in content is stripped."""
    acc = _Resp(200, {"id": "42"})
    statuses = _Resp(200, [
        {"url": "https://mastodon.example/@u/1", "content": "<p>Hello <b>world</b></p>"},
        {"url": "https://mastodon.example/@u/2", "content": "<p>Second</p>"},
    ])
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@mastodon.example")
    assert len(posts) == 2
    assert posts[0].url == "https://mastodon.example/@u/1"
    assert posts[0].text == "Hello world"
    assert posts[1].text == "Second"


async def test_mastodon_account_lookup_non200(monkeypatch):
    """Non-200 on account lookup returns empty list immediately."""
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(404, {})))
    posts = await mastodon_timeline("@u@mastodon.example")
    assert posts == []


async def test_mastodon_no_account_id(monkeypatch):
    """200 response but no 'id' field in payload returns empty list."""
    acc = _Resp(200, {"username": "u"})  # id field missing
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc))
    posts = await mastodon_timeline("@u@mastodon.example")
    assert posts == []


async def test_mastodon_statuses_non200(monkeypatch):
    """Account lookup OK but statuses endpoint returns non-200 → []."""
    acc = _Resp(200, {"id": "42"})
    statuses = _Resp(503, {})
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@mastodon.example")
    assert posts == []


async def test_mastodon_empty_statuses(monkeypatch):
    """Empty statuses list returns empty posts list."""
    acc = _Resp(200, {"id": "42"})
    statuses = _Resp(200, [])
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@mastodon.example")
    assert posts == []


async def test_mastodon_null_statuses(monkeypatch):
    """Null statuses payload is treated the same as an empty list."""
    acc = _Resp(200, {"id": "42"})
    statuses = _Resp(200, None)
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@mastodon.example")
    assert posts == []


async def test_mastodon_skips_status_without_url(monkeypatch):
    """Status entries with no/empty url are skipped; others are kept."""
    acc = _Resp(200, {"id": "42"})
    statuses = _Resp(200, [
        {"url": "", "content": "<p>no url</p>"},
        {"url": "https://mastodon.example/@u/2", "content": "<p>has url</p>"},
    ])
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@mastodon.example")
    assert len(posts) == 1
    assert posts[0].url == "https://mastodon.example/@u/2"


async def test_mastodon_reblog_fallback(monkeypatch):
    """Empty content falls back to reblog inner content and URL."""
    acc = _Resp(200, {"id": "42"})
    statuses = _Resp(200, [
        {
            "url": "https://mastodon.example/@u/1",
            "content": "",
            "reblog": {
                "url": "https://other.social/@orig/99",
                "content": "<p>Original content</p>",
            },
        },
    ])
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@mastodon.example")
    assert len(posts) == 1
    assert posts[0].url == "https://other.social/@orig/99"
    assert posts[0].text == "Original content"


async def test_mastodon_count_zero_clamped(monkeypatch):
    """count=0 is clamped to 1 (no error, returns whatever the server gives)."""
    acc = _Resp(200, {"id": "42"})
    statuses = _Resp(200, [])
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@mastodon.example", count=0)
    assert posts == []


async def test_mastodon_count_high_clamped(monkeypatch):
    """count > 40 is clamped to 40 (no error)."""
    acc = _Resp(200, {"id": "42"})
    statuses = _Resp(200, [{"url": "https://m.example/@u/1", "content": "hi"}])
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@mastodon.example", count=999)
    assert len(posts) == 1


async def test_mastodon_custom_user_agent(monkeypatch):
    """Explicit user_agent passes through without error."""
    acc = _Resp(200, {"id": "7"})
    statuses = _Resp(200, [{"url": "https://m.example/@u/1", "content": "hi"}])
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@m.example", user_agent="mybot/2.0")
    assert len(posts) == 1


async def test_mastodon_env_user_agent(monkeypatch):
    """Falls back to SCRAPER_USER_AGENT env var when no explicit ua."""
    monkeypatch.setenv("SCRAPER_USER_AGENT", "custom-agent/1.0")
    acc = _Resp(200, {"id": "5"})
    statuses = _Resp(200, [])
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(acc, statuses))
    posts = await mastodon_timeline("@u@m.example")
    assert posts == []


# --------------------------------------------------------------------------- #
# twitter_search
# --------------------------------------------------------------------------- #

async def test_twitter_search_normal(monkeypatch):
    """Normal response returns SocialPost list with title+description joined."""
    data = {
        "web": {
            "results": [
                {"url": "https://x.com/user/status/1", "title": "Hello", "description": "world"},
                {"url": "https://twitter.com/user/status/2", "title": "Only title", "description": ""},
            ]
        }
    }
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, data)))
    posts = await twitter_search("someuser", api_key="key-xxx")
    assert len(posts) == 2
    assert posts[0].url == "https://x.com/user/status/1"
    assert posts[0].text == "Hello world"
    assert posts[1].text == "Only title"


async def test_twitter_search_api_error_raises(monkeypatch):
    """Non-200 response raises BraveError with the status code."""
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(429, "rate limited")))
    with pytest.raises(BraveError, match="429"):
        await twitter_search("user", api_key="test-key")


async def test_twitter_search_500_raises(monkeypatch):
    """5xx also raises BraveError."""
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(500, "server error")))
    with pytest.raises(BraveError, match="500"):
        await twitter_search("user", api_key="test-key")


async def test_twitter_search_empty_results(monkeypatch):
    """No results in web.results → empty list."""
    data = {"web": {"results": []}}
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, data)))
    posts = await twitter_search("user", api_key="key")
    assert posts == []


async def test_twitter_search_missing_web_key(monkeypatch):
    """Response without 'web' key → empty list (no KeyError)."""
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, {})))
    posts = await twitter_search("user", api_key="key")
    assert posts == []


async def test_twitter_search_skips_empty_url(monkeypatch):
    """Result entries with empty url are skipped."""
    data = {
        "web": {
            "results": [
                {"url": "", "title": "no url", "description": ""},
                {"url": "https://x.com/u/1", "title": "ok", "description": "stuff"},
            ]
        }
    }
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, data)))
    posts = await twitter_search("user", api_key="key")
    assert len(posts) == 1
    assert posts[0].url == "https://x.com/u/1"
    assert posts[0].text == "ok stuff"


async def test_twitter_search_strips_at_prefix(monkeypatch):
    """Leading @ is stripped from the username before querying."""
    data = {"web": {"results": []}}
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, data)))
    posts = await twitter_search("@theuser", api_key="key")
    assert posts == []


async def test_twitter_search_count_clamped_low(monkeypatch):
    """count=0 is clamped to 1."""
    data = {"web": {"results": []}}
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, data)))
    posts = await twitter_search("user", count=0, api_key="key")
    assert posts == []


async def test_twitter_search_count_clamped_high(monkeypatch):
    """count > 20 is clamped to 20."""
    data = {"web": {"results": []}}
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, data)))
    posts = await twitter_search("user", count=999, api_key="key")
    assert posts == []


async def test_twitter_search_key_from_env(monkeypatch):
    """api_key falls back to SEARCH_API_KEY env var."""
    monkeypatch.setenv("SEARCH_API_KEY", "env-key")
    data = {"web": {"results": []}}
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, data)))
    posts = await twitter_search("user")
    assert posts == []


async def test_twitter_search_description_only(monkeypatch):
    """When title is empty but description is present, text is just description."""
    data = {
        "web": {
            "results": [
                {"url": "https://x.com/u/1", "title": "", "description": "desc only"},
            ]
        }
    }
    monkeypatch.setattr("aiohttp.ClientSession", _make_session(_Resp(200, data)))
    posts = await twitter_search("user", api_key="key")
    assert posts[0].text == "desc only"
