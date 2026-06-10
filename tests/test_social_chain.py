"""X-chain legs offline: syndication parsers, NitterPool health/scoring,
and the free→paid escalation logic in x_posts (legs monkeypatched)."""
from __future__ import annotations

import pytest

from ujin.sources.social._nitter import NitterPool
from ujin.sources.social._syndication import _from_html, _from_json
from ujin.sources.social.twitter import BraveNotConfigured, SocialPost
from ujin.sources.social.x import x_posts


# ── syndication parsers ──────────────────────────────────────────────────────

SYND_JSON = {
    "timeline": {"entries": [
        {"type": "tweet", "content": {"tweet": {
            "full_text": "first post text",
            "permalink_url": "https://twitter.com/u/status/111"}}},
        {"type": "ad", "content": {}},
        {"type": "tweet", "content": {"tweet": {
            "text": "second post text",
            "permalink": "https://x.com/u/status/222"}}},
        {"type": "tweet", "content": {"tweet": {
            "full_text": "no permalink so dropped"}}},
        {"type": "tweet", "content": {"tweet": {
            "full_text": "bad link", "permalink_url": "https://evil.test/x"}}},
    ]}
}


def test_from_json_filters_and_caps():
    posts = _from_json(SYND_JSON, count=10)
    assert [p.url for p in posts] == [
        "https://twitter.com/u/status/111", "https://x.com/u/status/222"]
    assert posts[0].text == "first post text"
    assert _from_json(SYND_JSON, count=1)[0].url.endswith("/111")


def test_from_json_falls_back_to_embedded_html():
    html = ('<div class="timeline-Tweet"><p class="timeline-Tweet-text">html '
            'leg text</p><a href="https://twitter.com/u/status/333">t</a></div>')
    posts = _from_json({"body": html}, count=5)
    assert len(posts) == 1 and posts[0].url.endswith("/333")


def test_from_html_rejects_junk():
    assert _from_html("", 5) == []
    assert _from_html("<div class='Tweet'><p></p></div>", 5) == []
    bad_link = ('<div class="Tweet"><p>text here</p>'
                '<a href="https://evil.test/status/9">x</a></div>')
    assert _from_html(bad_link, 5) == []


# ── nitter pool ──────────────────────────────────────────────────────────────

def test_pool_from_list_and_yaml(tmp_path):
    pool = NitterPool.from_list(["https://n1.test/", "https://n2.test"])
    assert [m.base for m in pool.mirrors] == ["https://n1.test", "https://n2.test"]

    yml = tmp_path / "pool.yaml"
    yml.write_text("mirrors:\n  - https://n3.test\n")
    assert [m.base for m in NitterPool.from_yaml(str(yml)).mirrors] == ["https://n3.test"]
    assert NitterPool.from_yaml(str(tmp_path / "missing.yaml")).mirrors == []


def test_pool_failure_scoring_and_cooldown():
    pool = NitterPool.from_list(["https://n1.test"])
    m = pool.mirrors[0]
    assert pool.healthy() == [m]
    for _ in range(10):
        pool.record_failure(m)
    assert pool.healthy() == []          # cooled down
    assert m.score < 1.0

    pool.record_success(m, latency_ms=120.0)
    assert m.score == 1.0
    status = pool.status()[0]
    assert status["successes"] == 1 and status["failures"] == 10
    assert status["last_latency_ms"] == 120.0


# ── the chain ────────────────────────────────────────────────────────────────

def _posts(leg):
    return [SocialPost(url=f"https://x.com/u/status/{leg}", text=f"{leg} post")]


@pytest.fixture
def legs(monkeypatch):
    """Patch all three legs; each test configures their behavior."""
    state = {"nitter": [], "synd": [], "brave": [], "calls": []}

    async def nitter(pool, username, count):
        state["calls"].append("nitter")
        return state["nitter"]

    async def synd(username, count):
        state["calls"].append("synd")
        if isinstance(state["synd"], Exception):
            raise state["synd"]
        return state["synd"]

    async def brave(username, count):
        state["calls"].append("brave")
        if isinstance(state["brave"], Exception):
            raise state["brave"]
        return state["brave"]

    monkeypatch.setattr("ujin.sources.social.x.nitter_posts", nitter)
    monkeypatch.setattr("ujin.sources.social.x.syndication_posts", synd)
    monkeypatch.setattr("ujin.sources.social.x.twitter_search", brave)
    return state


async def test_chain_nitter_wins_when_it_returns(legs):
    legs["nitter"] = _posts("nitter")
    result = await x_posts("user", nitter=NitterPool.from_list(["https://n.test"]))
    assert result.leg == "nitter"
    assert legs["calls"] == ["nitter"]   # later legs never consulted


async def test_chain_falls_to_syndication(legs):
    legs["synd"] = _posts("synd")
    result = await x_posts("user")       # no nitter pool at all
    assert result.leg == "syndication"
    assert legs["calls"] == ["synd"]


async def test_chain_brave_last_and_gated(legs):
    legs["brave"] = _posts("brave")
    result = await x_posts("user")
    assert result.leg == "brave"

    legs["calls"].clear()
    result = await x_posts("user", allow_brave=False)
    assert result.leg == "empty"
    assert "brave" not in legs["calls"]

    legs["calls"].clear()
    result = await x_posts("user", brave_gate=lambda: False)  # budget says no
    assert result.leg == "empty"
    assert "brave" not in legs["calls"]


async def test_chain_brave_not_configured_is_empty(legs):
    legs["brave"] = BraveNotConfigured("no key")
    result = await x_posts("user")
    assert result.leg == "empty" and result.posts == []


async def test_chain_empty_username_short_circuits(legs):
    result = await x_posts("@")
    assert result.leg == "empty"
    assert legs["calls"] == []
