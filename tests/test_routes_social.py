"""Social/trends route surface with the source functions monkeypatched —
exercises wire shapes and the graceful-degradation paths."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import ujin.scrape.routes_social as rs  # noqa: E402
from ujin.scrape.app import create_scrape_app  # noqa: E402
from ujin.scrape.config import ScrapeConfig  # noqa: E402
from ujin.sources.social import BraveError, BraveNotConfigured  # noqa: E402


class _Post:
    def __init__(self, url, text):
        self.url, self.text = url, text


@pytest.fixture
def client():
    app = create_scrape_app(ScrapeConfig())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def test_twitter_without_brave_key_503(client, monkeypatch):
    async def fake(username, count, api_key=None):
        raise BraveNotConfigured("no brave key")

    monkeypatch.setattr(rs, "twitter_search", fake)
    r = client.post("/social/twitter", json={"username": "someone"})
    assert r.status_code == 503


def test_twitter_brave_error_502(client, monkeypatch):
    async def fake(username, count, api_key=None):
        raise BraveError("rate limited")

    monkeypatch.setattr(rs, "twitter_search", fake)
    assert client.post("/social/twitter",
                       json={"username": "someone"}).status_code == 502


def test_twitter_success_shape(client, monkeypatch):
    async def fake(username, count, api_key=None):
        return [_Post("https://x.com/u/1", "hello world")]

    monkeypatch.setattr(rs, "twitter_search", fake)
    body = client.post("/social/twitter", json={"username": "u"}).json()
    assert body["posts"] == [{"url": "https://x.com/u/1", "text": "hello world"}]


def test_twitter_empty_username_400(client):
    assert client.post("/social/twitter", json={"username": ""}).status_code == 400


def test_mastodon_success_and_errors(client, monkeypatch):
    async def ok(account, count, user_agent=None):
        return [_Post("https://m.test/@a/1", "toot")]

    monkeypatch.setattr(rs, "mastodon_timeline", ok)
    body = client.post("/social/mastodon", json={"account": "@a@m.test"}).json()
    assert body["posts"][0]["text"] == "toot"

    async def bad_account(account, count, user_agent=None):
        raise ValueError("bad account format")

    monkeypatch.setattr(rs, "mastodon_timeline", bad_account)
    assert client.post("/social/mastodon",
                       json={"account": "junk"}).status_code == 400

    async def down(account, count, user_agent=None):
        raise RuntimeError("instance down")

    monkeypatch.setattr(rs, "mastodon_timeline", down)
    assert client.post("/social/mastodon",
                       json={"account": "@a@m.test"}).status_code == 502


def test_x_chain_reports_leg(client, monkeypatch):
    class _Result:
        leg = "syndication"
        posts = [_Post("https://x.com/u/2", "post body")]

    async def fake(username, count, nitter=None, allow_brave=True):
        return _Result()

    monkeypatch.setattr(rs, "x_posts", fake)
    body = client.post("/social/x", json={"username": "u"}).json()
    assert body["leg"] == "syndication"
    assert len(body["posts"]) == 1


def test_truth_route(client, monkeypatch):
    async def fake(username, count):
        return [_Post("https://truth.test/@u/1", "post")]

    monkeypatch.setattr(rs, "truth_social_posts", fake)
    body = client.post("/social/truth", json={"username": "u"}).json()
    assert body["posts"][0]["url"].endswith("/1")

    async def boom(username, count):
        raise RuntimeError("blocked")

    monkeypatch.setattr(rs, "truth_social_posts", boom)
    assert client.post("/social/truth", json={"username": "u"}).status_code == 502


def test_trends_x_route(client, monkeypatch):
    class _Item:
        rank, tag, url, volume = 1, "#topic", "https://x.com/t", "12K"

    class _Result:
        region = "united-states"
        items = [_Item()]
        source = "trends24"

    async def fake(region, count):
        return _Result()

    monkeypatch.setattr(rs, "fetch_x_trends", fake)
    body = client.post("/trends/x", json={"region": "united-states"}).json()
    assert body["items"][0]["tag"] == "#topic"
    assert body["source"] == "trends24"


def test_corroborated_empty_without_store(client):
    body = client.get("/trends/corroborated").json()
    assert body["clusters"] == []
    assert body["window_secs"] > 0


def test_corroborated_with_store_scoring(client):
    import time

    from ujin.trends import CorroborationStore

    store = CorroborationStore(window_secs=1800, max_entries=100,
                               min_hosts_for_corroboration=2,
                               max_hosts_for_full_score=4)
    now = time.time()
    for host in ("a.test", "b.test", "c.test", "d.test"):
        store.add(f"major event unfolding tonight", host=host, ts=now)
    store.add("only one host has this", host="solo.test", ts=now)

    client.app.state.corroboration = store
    body = client.get("/trends/corroborated").json()
    assert body["clusters"], "expected at least the corroborated cluster"
    top = body["clusters"][0]
    assert len(top["hosts"]) == 4
    # floor is interpolated from the app config's min/max host thresholds
    assert 0.5 <= top["breaking_score_floor"] <= 1.0
    solo = body["clusters"][-1]
    if len(solo["hosts"]) == 1:
        assert solo["breaking_score_floor"] == 0.0
