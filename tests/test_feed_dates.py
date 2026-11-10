"""Feed dates are ISO-8601 UTC, and combined mode never waits on a slow HTML leg
when a pinned feed has already answered (nexus live-run findings, 2026-11-09)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest
from conftest import FakeHttp
from test_cov_scrape import HOME, _index_html, _resp, _service

import ujin.scrape.service as svc_mod
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.host_overrides import HostOverride, HostOverrideRegistry
from ujin.sources import rss as rss_mod
from ujin.sources.rss import FeedItem

RSS2 = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>A</title><link>https://x.test/a</link>
<pubDate>Mon, 09 Nov 2026 16:38:00 -0500</pubDate></item>
<item><title>B</title><link>https://x.test/b</link></item>
<item><title>C</title><link>https://x.test/c</link><pubDate>not a date</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>A</title><link href="https://x.test/a"/><id>1</id>
<updated>2026-11-09T21:38:00Z</updated></entry></feed>"""


@pytest.fixture
def feed_text(monkeypatch):
    """Make parse_feed read an in-memory document instead of the network."""
    real = rss_mod.feedparser.parse

    def _set(text):
        monkeypatch.setattr(rss_mod.feedparser, "parse", lambda url: real(text))

    return _set


async def test_rfc2822_pubdate_becomes_iso_utc(feed_text):
    feed_text(RSS2)
    a, b, c = await rss_mod.parse_feed("https://x.test/feed")
    assert a.published == "2026-11-09T21:38:00+00:00"
    assert datetime.fromisoformat(a.published).utcoffset().total_seconds() == 0
    assert b.published is None


async def test_unparseable_date_keeps_raw_string(feed_text):
    feed_text(RSS2)
    *_, c = await rss_mod.parse_feed("https://x.test/feed")
    assert c.published == "not a date"


async def test_atom_updated_is_used_and_normalized(feed_text):
    feed_text(ATOM)
    (a,) = await rss_mod.parse_feed("https://x.test/feed")
    assert a.published == "2026-11-09T21:38:00+00:00"


async def test_feed_route_emits_published_and_published_at(feed_text):
    from fastapi.testclient import TestClient

    from ujin.scrape.app import create_scrape_app

    feed_text(RSS2)
    with TestClient(create_scrape_app()) as client:
        items = client.post("/feed", json={"url": "https://x.test/feed"}).json()["items"]
    assert items[0]["published_at"] == items[0]["published"] == "2026-11-09T21:38:00+00:00"
    assert items[1]["published_at"] is None


def _pinned(rss_url="https://news.example.com/feed.xml"):
    return HostOverrideRegistry({"news.example.com": HostOverride(rss_url=rss_url)})


async def test_combined_returns_feed_when_html_leg_is_slow(monkeypatch):
    async def feed(url):
        return [FeedItem("https://news.example.com/2026/06/09/from-feed",
                         "A real headline from the pinned feed", "", "2026-11-09T21:38:00+00:00")]

    class SlowHttp(FakeHttp):
        async def get(self, *a, **k):
            await asyncio.sleep(30)

    monkeypatch.setattr(svc_mod, "parse_feed", feed, raising=False)
    monkeypatch.setattr(rss_mod, "parse_feed", feed)
    svc = _service(SlowHttp({}), overrides=_pinned(),
                   config=ScrapeConfig(combined_html_timeout_secs=1))
    t0 = time.monotonic()
    r = await svc.scrape(HOME, mode="combined")
    assert time.monotonic() - t0 < 5
    assert [l.published for l in r.links] == ["2026-11-09T21:38:00+00:00"]
    assert "html_err=TimeoutError" in (r.note or "")


async def test_combined_cap_is_a_total_budget_not_added_after_a_slow_feed(monkeypatch):
    async def slow_feed(url):
        await asyncio.sleep(1.2)
        return [FeedItem("https://news.example.com/2026/06/09/slow-feed-story",
                         "A real headline from a slow pinned feed", "", None)]

    class SlowHttp(FakeHttp):
        async def get(self, *a, **k):
            await asyncio.sleep(30)

    monkeypatch.setattr(svc_mod, "parse_feed", slow_feed, raising=False)
    monkeypatch.setattr(rss_mod, "parse_feed", slow_feed)
    svc = _service(SlowHttp({}), overrides=_pinned(),
                   config=ScrapeConfig(combined_html_timeout_secs=2))
    t0 = time.monotonic()
    r = await svc.scrape(HOME, mode="combined")
    assert time.monotonic() - t0 < 2.6  # 1.2s feed + <=0.8s left of a 2s budget, not 1.2 + 2
    assert len(r.links) == 1


async def test_combined_without_pin_still_waits_for_html():
    http = FakeHttp({HOME: _resp(HOME, _index_html(n=6))})
    svc = _service(http, config=ScrapeConfig(combined_html_timeout_secs=1))
    r = await svc.scrape(HOME, mode="combined")
    assert len(r.links) >= 5 and "html_err" not in (r.note or "")


def test_timeout_env_knob():
    cfg = ScrapeConfig.from_env({"UJIN_COMBINED_HTML_TIMEOUT_SECS": "7"})
    assert cfg.combined_html_timeout_secs == 7
    assert ScrapeConfig().combined_html_timeout_secs == 10
