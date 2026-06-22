"""Coverage gap-fill for:
  ujin/trends/corroboration.py  (target ≥88%)
  ujin/trends/scorer.py         (target ≥88%)
  ujin/mcp/server.py            (target ≥88%)
  ujin/service.py               (target ≥88%)
  ujin/sources/social/x.py     (target ≥88%)

All offline — no real network calls; fakes / monkeypatching only.
"""
from __future__ import annotations

import asyncio

import pytest

# ── ujin/trends/corroboration.py ─────────────────────────────────────────────

from ujin.trends.corroboration import (  # noqa: E402
    Cluster,
    CorroborationStore,
    jaccard,
    shingleset,
)


def test_shingleset_short_text_falls_back_to_unigrams():
    # text with < n content tokens after stop-word filtering → frozenset(toks) (line 51)
    result = shingleset("senate")  # single content token
    assert isinstance(result, frozenset)
    assert "senate" in result


def test_jaccard_zero_intersection():
    # no shared tokens → inter == 0 → 0.0 (line 67)
    a = frozenset(["alpha bravo", "charlie delta"])
    b = frozenset(["zulu yankee", "whiskey victor"])
    assert jaccard(a, b) == 0.0


def test_velocity_per_min_zero_span():
    # last_seen_ts <= first_seen_ts → 0.0 early return (lines 88-89)
    c = Cluster(
        representative="headline",
        hosts={"h1", "h2"},
        members=[],
        first_seen_ts=1000.0,
        last_seen_ts=1000.0,
    )
    assert c.velocity_per_min() == 0.0


def test_velocity_per_min_positive_span():
    # span > 0 → non-zero velocity (lines 90-91)
    c = Cluster(
        representative="headline",
        hosts={"h1", "h2"},
        members=[],
        first_seen_ts=1000.0,
        last_seen_ts=1060.0,  # 1-minute span
    )
    assert c.velocity_per_min() > 0.0


def test_add_empty_text_or_host_is_noop():
    # empty text or host → early return (line 117)
    store = CorroborationStore()
    store.add("", "host.com")
    store.add("Some real headline content", "")
    assert store.size() == 0


def test_add_stopwords_only_produces_empty_shingles():
    # all stopwords → shingleset is empty → return (line 121)
    store = CorroborationStore()
    store.add("the a an of in", "host.com")
    assert store.size() == 0


def test_evict_old_pops_stale_entries():
    # second add triggers _evict_old which poplefts the old entry (line 128)
    store = CorroborationStore(window_secs=10.0)
    store.add("old headline text here", "host.com", ts=1000.0)
    assert store.size() == 1
    store.add("new headline text recent", "host2.com", ts=2000.0)
    assert store.size() == 1  # old entry evicted


def test_lookup_score_empty_shingles_returns_zero():
    # text tokenises to nothing → (0.0, 0) (line 140)
    store = CorroborationStore()
    score, n = store.lookup_score("the a an", "host.com")
    assert score == 0.0 and n == 0


def test_lookup_score_below_threshold_in_store():
    # entries exist but jaccard < threshold → loop branch not taken (line 143->142),
    # only {host} in hosts → n < min_hosts → (0.0, n) (line 147)
    store = CorroborationStore(
        min_hosts_for_corroboration=3,
        jaccard_threshold=0.5,
    )
    store.add("completely unrelated zebra topic", "other.com", ts=1000.0)
    score, n = store.lookup_score("senate finance committee tax bill vote", "host.com")
    assert score == 0.0
    assert n == 1  # only the query host; unrelated entry didn't cross threshold


def test_lookup_score_max_hosts_full_score():
    # n >= max_hosts → 1.0 (line 149)
    store = CorroborationStore(
        min_hosts_for_corroboration=2,
        max_hosts_for_full_score=3,
    )
    headline = "breaking senate vote passes spending bill tonight"
    store.add(headline, "h1.com", ts=1000.0)
    store.add(headline, "h2.com", ts=1000.0)
    store.add(headline, "h3.com", ts=1000.0)
    score, n = store.lookup_score(headline, "h4.com")  # 4 unique hosts ≥ max (3)
    assert score == 1.0
    assert n >= 3


def test_clusters_places_and_updates_representative():
    # second entry matches first cluster → placed=True (line 166->165),
    # longer text becomes representative (line 172)
    store = CorroborationStore(
        min_hosts_for_corroboration=2,
        jaccard_threshold=0.20,
    )
    # Use add() without ts so entries get time.time() and are inside the window.
    store.add("senate passes spending bill vote", "h1.com")
    store.add(
        "senate passes spending bill in a late-night historic vote",
        "h2.com",
    )
    clusters = store.clusters()
    assert len(clusters) >= 1
    c = clusters[0]
    assert len(c.hosts) >= 2
    assert "late-night" in c.representative  # longer text won


def test_size_and_iter_recent():
    # size() (line 190) and iter_recent() (line 193)
    store = CorroborationStore()
    store.add("senate passes major spending bill", "h1.com", ts=1000.0)
    store.add("market crash hits record low today", "h2.com", ts=1001.0)
    assert store.size() == 2
    recent = list(store.iter_recent(n=1))
    assert len(recent) == 1
    assert recent[0].host == "h2.com"


# ── ujin/trends/scorer.py ─────────────────────────────────────────────────────

from ujin.extract.links import NormalizedLink  # noqa: E402
from ujin.trends import BreakingScorer  # noqa: E402


def test_scorer_trend_terms_provider_exception_is_swallowed():
    # provider raises → caught, terms=[] (lines 46-49)
    def bad_provider():
        raise RuntimeError("quota exceeded")

    scorer = BreakingScorer(trend_terms_provider=bad_provider)
    links = [NormalizedLink(url="https://example.com/a", text="Some real headline text")]
    scorer.score_links(links, base_url="https://example.com/")
    assert hasattr(links[0], "_breaking_score")  # annotation still applied


def test_scorer_corroboration_add_exception_is_swallowed():
    # corroboration.add raises → caught silently (lines 76-79)
    class BrokenStore:
        def add(self, text, host):
            raise ValueError("broken store")

        def lookup_score(self, text, host):
            return 0.0, 0

    scorer = BreakingScorer(corroboration=BrokenStore())
    links = [NormalizedLink(url="https://example.com/b", text="Breaking story content here")]
    scorer.score_links(links, base_url="https://example.com/")
    assert hasattr(links[0], "_breaking_score")


# ── ujin/sources/social/x.py ─────────────────────────────────────────────────

from ujin.sources.social._nitter import NitterPool  # noqa: E402
from ujin.sources.social.twitter import BraveError, SocialPost  # noqa: E402
from ujin.sources.social.x import x_posts  # noqa: E402


def _x_posts(leg: str) -> list[SocialPost]:
    return [SocialPost(url=f"https://x.com/u/status/{leg}", text=f"{leg} post")]


@pytest.fixture
def x_legs(monkeypatch):
    """Monkeypatched chain legs; configure return values in each test."""
    state: dict = {"nitter": [], "synd": [], "brave": [], "calls": []}

    async def fake_nitter(pool, username, count):
        state["calls"].append("nitter")
        if isinstance(state["nitter"], Exception):
            raise state["nitter"]
        return state["nitter"]

    async def fake_synd(username, count):
        state["calls"].append("synd")
        if isinstance(state["synd"], Exception):
            raise state["synd"]
        return state["synd"]

    async def fake_brave(username, count):
        state["calls"].append("brave")
        if isinstance(state["brave"], Exception):
            raise state["brave"]
        return state["brave"]

    monkeypatch.setattr("ujin.sources.social.x.nitter_posts", fake_nitter)
    monkeypatch.setattr("ujin.sources.social.x.syndication_posts", fake_synd)
    monkeypatch.setattr("ujin.sources.social.x.twitter_search", fake_brave)
    return state


async def test_x_nitter_exception_falls_to_syndication(x_legs):
    # nitter raises → except block sets posts=[] (lines 55-57), falls to synd
    x_legs["nitter"] = RuntimeError("mirror crashed")
    x_legs["synd"] = _x_posts("synd")
    pool = NitterPool.from_list(["https://n.test"])
    result = await x_posts("user", nitter=pool)
    assert result.leg == "syndication"
    assert "nitter" in x_legs["calls"]
    assert "synd" in x_legs["calls"]


async def test_x_nitter_empty_falls_to_syndication(x_legs):
    # nitter returns [] → if posts: False (line 58->61), falls to synd
    x_legs["nitter"] = []
    x_legs["synd"] = _x_posts("synd")
    pool = NitterPool.from_list(["https://n.test"])
    result = await x_posts("user", nitter=pool)
    assert result.leg == "syndication"


async def test_x_syndication_exception_falls_to_brave(x_legs):
    # syndication raises → except block (lines 63-65), falls to brave
    x_legs["synd"] = RuntimeError("syndication down")
    x_legs["brave"] = _x_posts("brave")
    result = await x_posts("user")
    assert result.leg == "brave"
    assert "synd" in x_legs["calls"]
    assert "brave" in x_legs["calls"]


async def test_x_brave_error_returns_empty(x_legs):
    # BraveError raised → lines 74-76 → empty result
    x_legs["brave"] = BraveError("api error 500")
    result = await x_posts("user")
    assert result.leg == "empty"
    assert result.posts == []


async def test_x_brave_empty_list_returns_empty(x_legs):
    # brave returns [] → if posts: False (line 77->80) → final empty return
    x_legs["brave"] = []
    result = await x_posts("user")
    assert result.leg == "empty"
    assert result.posts == []


# ── ujin/mcp/server.py ───────────────────────────────────────────────────────

mcp_sdk = pytest.importorskip("mcp")

from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session as _mcp_client_session,
)

from ujin.mcp.server import _Backend, create_mcp_server  # noqa: E402


async def test_backend_stop_calls_closers():
    # stop() invokes both _scrape_close and _store.close (lines 69-72)
    b = _Backend()
    scrape_closed: list = []
    store_closed: list = []

    async def fake_scrape_close():
        scrape_closed.append(1)

    class _FakeStore:
        def close(self):
            store_closed.append(1)

    b._scrape_close = fake_scrape_close
    b._store = _FakeStore()
    await b.stop()
    assert scrape_closed == [1]
    assert store_closed == [1]


async def test_backend_stop_noop_when_attrs_are_none():
    # stop() with unset attributes is safe (covers the None-guard branches)
    b = _Backend()
    await b.stop()  # must not raise


async def test_backend_start(monkeypatch):
    # start() wires up ScrapeService + JobManager (lines 50-66)
    import ujin.scrape.build as _sb
    import ujin.jobs.store as _js
    import ujin.jobs.manager as _jm

    fake_service = object()
    closed: list = []

    async def fake_close():
        closed.append(1)

    async def fake_build(config):
        return fake_service, None, fake_close

    class _FakeStore:
        def __init__(self, path):
            pass

        def close(self):
            pass

    class _FakeManager:
        def __init__(self, engine, store, scrape_service=None):
            pass

        def load_from_store(self):
            pass

    monkeypatch.setattr(_sb, "build_scrape_service", fake_build)
    monkeypatch.setattr(_js, "JobStore", _FakeStore)
    monkeypatch.setattr(_jm, "JobManager", _FakeManager)

    b = _Backend()
    await b.start()
    assert b.scrape_service is fake_service
    assert b._scrape_close is fake_close
    assert b.manager is not None


async def test_lifespan_owned_start_and_stop(monkeypatch):
    # lifespan with owned=True calls start() and stop() (lines 86, 91)
    started: list = []
    stopped: list = []

    async def fake_start(self):
        started.append(1)

    async def fake_stop(self):
        stopped.append(1)

    monkeypatch.setattr(_Backend, "start", fake_start)
    monkeypatch.setattr(_Backend, "stop", fake_stop)

    server = create_mcp_server()  # no backend → owned=True
    async with _mcp_client_session(server._mcp_server) as client:
        tools = await client.list_tools()
        assert tools.tools  # at least one tool listed

    assert started == [1]
    assert stopped == [1]


@pytest.fixture
def _mcp_backend(tmp_path):
    """Minimal pre-built backend for MCP tool-level tests (no network)."""
    from conftest import FakeHttp, FakeObscura
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.engine import PollEngine
    from ujin.fetch.http import HttpResponse
    from ujin.jobs.manager import JobManager
    from ujin.jobs.store import JobStore
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    HOME = "https://feed.example.com/"
    b = _Backend()
    b.scrape_service = ScrapeService(
        http=FakeHttp({HOME: HttpResponse(url=HOME, status=200, body="", final_url=HOME)}),
        obscura=FakeObscura(),
        cache=ScrapeCache(),
        policy=HostPolicy(cooldown_secs=60),
        config=ScrapeConfig(),
    )
    b.manager = JobManager(
        PollEngine(),
        JobStore(tmp_path / "mcp_test.db"),
        scrape_service=b.scrape_service,
    )
    return b


async def test_scrape_feed_tool(monkeypatch, _mcp_backend):
    # scrape_feed imports parse_feed at call-time and returns items (lines 131-134)
    import json

    from ujin.sources.rss import FeedItem

    async def fake_parse_feed(url, **kw):
        return [FeedItem(url="https://x.com/1", title="Item One", summary="")]

    monkeypatch.setattr("ujin.sources.rss.parse_feed", fake_parse_feed)

    server = create_mcp_server(_mcp_backend)
    async with _mcp_client_session(server._mcp_server) as client:
        result = await client.call_tool(
            "scrape_feed", {"url": "https://feed.example.com/rss"}
        )
    assert not result.isError
    body = json.loads(result.content[0].text)
    assert len(body["items"]) == 1
    assert body["items"][0]["url"] == "https://x.com/1"


async def test_discover_site_tool(monkeypatch, _mcp_backend):
    # discover_site calls discover_sources via runtime import (lines 140-145)
    import json

    async def fake_discover_sources(http, homepage):
        return {"feeds": [], "sitemaps": []}

    monkeypatch.setattr(
        "ujin.sources.discover.discover_sources", fake_discover_sources
    )

    server = create_mcp_server(_mcp_backend)
    async with _mcp_client_session(server._mcp_server) as client:
        result = await client.call_tool(
            "discover_site", {"homepage": "https://feed.example.com/"}
        )
    assert not result.isError
    body = json.loads(result.content[0].text)
    assert "feeds" in body or "sitemaps" in body or body == {}


def test_mcp_serve_stdio(monkeypatch):
    # serve() stdio branch (lines 220-221, 226)
    from ujin.mcp import server as mcp_mod

    run_calls: list = []

    class _FakeMCP:
        class settings:
            host = "127.0.0.1"
            port = 8903

        def run(self, transport):
            run_calls.append(transport)

    monkeypatch.setattr(mcp_mod, "create_mcp_server", lambda: _FakeMCP())
    mcp_mod.serve(transport="stdio")
    assert run_calls == ["stdio"]


def test_mcp_serve_http(monkeypatch):
    # serve() http branch (lines 221-225)
    from ujin.mcp import server as mcp_mod

    run_calls: list = []

    class _FakeSettings:
        host: str = ""
        port: int = 0

    class _FakeMCP:
        settings = _FakeSettings()

        def run(self, transport):
            run_calls.append(transport)

    instance = _FakeMCP()
    monkeypatch.setattr(mcp_mod, "create_mcp_server", lambda: instance)
    mcp_mod.serve(transport="http", host="0.0.0.0", port=1234)
    assert run_calls == ["streamable-http"]
    assert instance.settings.host == "0.0.0.0"
    assert instance.settings.port == 1234


# ── ujin/service.py ──────────────────────────────────────────────────────────

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from ujin.service import _Hub, create_app  # noqa: E402


async def test_hub_broadcast_event_removes_dead_connection():
    # send_json raises → appended to dead → discarded from _conns (lines 77-78, 80)
    hub = _Hub()

    class _BadWS:
        async def send_json(self, event):
            raise RuntimeError("connection lost")

    ws = _BadWS()
    hub.add(ws)
    assert ws in hub._conns
    await hub.broadcast_event({"event": "test"})
    assert ws not in hub._conns


async def test_hub_broadcast_event_good_and_bad_connections():
    # good ws survives; bad ws removed (covers both branches together)
    hub = _Hub()
    received: list = []

    class _GoodWS:
        async def send_json(self, event):
            received.append(event)

    class _BadWS:
        async def send_json(self, event):
            raise ConnectionError("dead socket")

    good = _GoodWS()
    bad = _BadWS()
    hub.add(good)
    hub.add(bad)
    await hub.broadcast_event({"event": "ping"})
    assert len(received) == 1
    assert bad not in hub._conns


def test_service_lifespan_run_engine_cancels_task():
    # run_engine=True → asyncio.Task created, cancelled on shutdown (lines 118-122)
    app = create_app(run_engine=True)  # no targets; engine idles, gets cancelled on exit
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
    # TestClient.__exit__ tears down the lifespan; CancelledError is caught (line 122)


def test_content_404_when_key_missing():
    # /content for an unknown key → 404 (lines 192-194)
    app = create_app(run_engine=False)
    with TestClient(app) as client:
        r = client.get("/content", params={"key": "https://missing.example.com/"})
    assert r.status_code == 404


def test_content_404_before_first_sweep():
    # /content after add but before sweep → prev is None → 404 (lines 192-194)
    app = create_app(run_engine=False)
    with TestClient(app) as client:
        resp = client.post(
            "/targets",
            json={"kind": "command", "config": {"argv": ["printf", "body_text"]}},
        )
        key = resp.json()["key"]
        r = client.get("/content", params={"key": key})
    assert r.status_code == 404


def test_content_200_after_sweep():
    # /content after sweep → prev set → 200 with content (lines 195-204)
    app = create_app(run_engine=False)
    with TestClient(app) as client:
        resp = client.post(
            "/targets",
            json={"kind": "command", "config": {"argv": ["printf", "body_text"]}},
        )
        key = resp.json()["key"]
        client.post("/sweep")
        r = client.get("/content", params={"key": key})
    assert r.status_code == 200
    data = r.json()
    assert data["key"] == key
    assert "fingerprint" in data
    assert "changed" in data


def test_wire_with_existing_async_on_change(monkeypatch):
    # Pre-existing target with on_change set → _wire preserves prev_cb (line 109),
    # sweep invokes _cb → prev_cb (async) is awaited (lines 100-103).
    from ujin.engine import PollEngine
    from ujin.poll.command import CommandPollable

    engine = PollEngine()
    pollable = CommandPollable(argv=["printf", "hello"])
    target = engine.add(pollable, base=60)

    prev_calls: list = []

    async def async_prev_cb(key, result):
        prev_calls.append(key)

    target.on_change = async_prev_cb

    def fake_load(path):
        return engine

    monkeypatch.setattr("ujin.cli._load", fake_load)

    app = create_app(config_path="fake.yaml", run_engine=False)
    with TestClient(app) as client:
        client.post("/sweep")  # fires _cb → prev_cb (async) → hub.broadcast

    assert prev_calls, "async prev_cb should have been awaited by _cb"


def test_service_serve_calls_uvicorn(monkeypatch):
    # serve() builds app and runs uvicorn (lines 224-226)
    import uvicorn
    import ujin.service as svc_mod

    run_calls: list = []
    monkeypatch.setattr(svc_mod, "create_app", lambda path: "fake-app")
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: run_calls.append((host, port)))
    svc_mod.serve(host="127.0.0.1", port=9999)
    assert run_calls == [("127.0.0.1", 9999)]
