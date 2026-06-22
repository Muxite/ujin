"""Offline unit tests for GraphQLPollable and its registry entry."""
from __future__ import annotations

import sys

import pytest

from ujin.poll.graphql import GraphQLPollable
from ujin.registry import register

QUERY = "{ users { id name } }"


# ── stub helpers ──────────────────────────────────────────────────────────────

def make_fetcher(status, data=None, exc=None):
    async def fetch(url, payload, headers, timeout):
        if exc is not None:
            raise exc
        return status, data
    return fetch


# ── data_path extraction ──────────────────────────────────────────────────────

async def test_graphql_data_path_extracts_slice():
    data = {"data": {"users": [{"id": 1, "name": "Alice"}]}}
    p = GraphQLPollable(
        "http://gql.test/", query=QUERY, data_path="data.users",
        _fetcher=make_fetcher(200, data),
    )
    r = await p.poll(None)
    assert r.ok and r.changed
    assert r.payload == [{"id": 1, "name": "Alice"}]
    assert r.status == 200


async def test_graphql_no_data_path_returns_full_response():
    data = {"data": {"count": 5}}
    p = GraphQLPollable("http://gql.test/", query=QUERY,
                        _fetcher=make_fetcher(200, data))
    r = await p.poll(None)
    assert r.ok and r.payload == data


async def test_graphql_stable_slice_not_changed():
    data = {"data": {"users": [1, 2]}, "ts": "ignored"}
    p = GraphQLPollable(
        "http://gql.test/", query=QUERY, data_path="data.users",
        _fetcher=make_fetcher(200, data),
    )
    r1 = await p.poll(None)
    r2 = await p.poll(r1)
    assert r2.ok and not r2.changed


# ── GraphQL errors array ──────────────────────────────────────────────────────

async def test_graphql_errors_dict_messages():
    data = {"errors": [{"message": "Not found"}, {"message": "Forbidden"}]}
    p = GraphQLPollable("http://gql.test/", query=QUERY,
                        _fetcher=make_fetcher(200, data))
    r = await p.poll(None)
    assert r.ok is False
    assert "Not found" in r.error
    assert "Forbidden" in r.error


async def test_graphql_errors_non_dict_entry():
    # A non-dict error entry falls back to str()
    data = {"errors": ["something went wrong"]}
    p = GraphQLPollable("http://gql.test/", query=QUERY,
                        _fetcher=make_fetcher(200, data))
    r = await p.poll(None)
    assert r.ok is False
    assert "something went wrong" in r.error


# ── non-200 status ────────────────────────────────────────────────────────────

async def test_graphql_5xx_is_failure():
    p = GraphQLPollable("http://gql.test/", query=QUERY,
                        _fetcher=make_fetcher(503, {}))
    r = await p.poll(None)
    assert r.ok is False
    assert r.status == 503
    assert "503" in r.error


async def test_graphql_429_is_failure():
    p = GraphQLPollable("http://gql.test/", query=QUERY,
                        _fetcher=make_fetcher(429, {}))
    r = await p.poll(None)
    assert r.ok is False
    assert r.status == 429


# ── network exception ─────────────────────────────────────────────────────────

async def test_graphql_network_exception():
    p = GraphQLPollable(
        "http://gql.test/", query=QUERY,
        _fetcher=make_fetcher(None, exc=ConnectionError("no route to host")),
    )
    r = await p.poll(None)
    assert r.ok is False
    assert "ConnectionError" in r.error


# ── missing aiohttp ───────────────────────────────────────────────────────────

async def test_graphql_missing_aiohttp(monkeypatch):
    monkeypatch.setitem(sys.modules, "aiohttp", None)
    p = GraphQLPollable("http://gql.test/", query=QUERY)
    r = await p.poll(None)
    assert r.ok is False
    assert "aiohttp required" in r.error


# ── aiohttp fetch path (monkeypatched) ────────────────────────────────────────

async def test_graphql_aiohttp_fetch_path(monkeypatch):
    import aiohttp

    class _Resp:
        status = 200

        async def json(self, content_type=None):
            return {"data": {"items": [42]}}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class _Session:
        def __init__(self, **kw):
            pass

        def post(self, url, **kw):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr("aiohttp.ClientSession", lambda **kw: _Session())
    p = GraphQLPollable("http://gql.test/", query=QUERY, data_path="data.items")
    r = await p.poll(None)
    assert r.ok and r.payload == [42]


# ── non-dict response body ────────────────────────────────────────────────────

async def test_graphql_non_dict_response():
    # A list response has no 'errors' key — treated as data
    p = GraphQLPollable("http://gql.test/", query=QUERY,
                        _fetcher=make_fetcher(200, [1, 2, 3]))
    r = await p.poll(None)
    assert r.ok and r.payload == [1, 2, 3]


# ── variables forwarded in payload ────────────────────────────────────────────

async def test_graphql_variables_forwarded():
    captured: dict = {}

    async def fetch(url, payload, headers, timeout):
        captured.update(payload)
        return 200, {"data": {}}

    p = GraphQLPollable(
        "http://gql.test/", query=QUERY,
        variables={"limit": 10, "offset": 0},
        _fetcher=fetch,
    )
    await p.poll(None)
    assert captured["variables"] == {"limit": 10, "offset": 0}


async def test_graphql_no_variables_not_in_payload():
    captured: dict = {}

    async def fetch(url, payload, headers, timeout):
        captured.update(payload)
        return 200, {"data": {}}

    p = GraphQLPollable("http://gql.test/", query=QUERY, _fetcher=fetch)
    await p.poll(None)
    assert "variables" not in captured


# ── custom key ────────────────────────────────────────────────────────────────

def test_graphql_custom_key():
    p = GraphQLPollable("http://gql.test/", query=QUERY, key="my-key",
                        _fetcher=make_fetcher(200, {}))
    assert p.key == "my-key"


def test_graphql_default_key_includes_url_and_data_path():
    p = GraphQLPollable("http://gql.test/", query=QUERY, data_path="data.items",
                        _fetcher=make_fetcher(200, {}))
    assert "gql.test" in p.key
    assert "data.items" in p.key


# ── registry ─────────────────────────────────────────────────────────────────

def test_graphql_registered_in_registry():
    assert register.has("source", "graphql")


def test_graphql_registry_builds_pollable():
    p = register.build_source("graphql", {
        "url": "http://gql.test/",
        "query": QUERY,
        "data_path": "data.items",
    })
    assert isinstance(p, GraphQLPollable)
    assert p.url == "http://gql.test/"
    assert p.query == QUERY
    assert p.data_path == "data.items"


def test_graphql_registry_optional_fields():
    p = register.build_source("graphql", {
        "url": "http://gql.test/",
        "query": QUERY,
        "variables": {"page": 1},
        "headers": {"Authorization": "Bearer tok"},
    })
    assert p.variables == {"page": 1}
    assert p.headers == {"Authorization": "Bearer tok"}
