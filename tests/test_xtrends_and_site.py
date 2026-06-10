"""x-trends source-chain parsing (mocked sessions) and SitePollable regions
against the fake origin."""
from __future__ import annotations

import pytest

from ujin.fetch.http import HttpFetcher
from ujin.poll.site import SitePollable
from ujin.sources.social.x_trends import (
    _from_getdaytrends,
    _from_trends24,
    fetch_x_trends,
)

TRENDS24_HTML = """<html><body>
<div class="trend-card"><ol class="trend-card__list">
  <li><a href="https://x.com/t/one">#one</a><span class="tweet-count">12K</span></li>
  <li><a href="https://x.com/t/two">#two</a></li>
  <li><span>no anchor</span></li>
  <li><a href="https://x.com/t/blank">  </a></li>
</ol></div>
</body></html>"""

GETDAYTRENDS_HTML = """<html><body>
<table class="ranking"><tbody>
  <tr><td><a class="trend-link" href="/trend/a">#alpha</a></td></tr>
  <tr><td><a class="trend-link" href="/trend/b">#beta</a></td></tr>
</tbody></table>
</body></html>"""


class _Resp:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, mapping):
        self._mapping = mapping  # url-substring -> _Resp | Exception

    def get(self, url):
        for frag, r in self._mapping.items():
            if frag in url:
                if isinstance(r, Exception):
                    raise r
                return r
        return _Resp(status=404)


async def test_trends24_parses_card():
    items = await _from_trends24(_Session({"trends24": _Resp(text=TRENDS24_HTML)}),
                                 "united-states", 10)
    assert [i.tag for i in items] == ["#one", "#two"]
    assert items[0].volume == "12K" and items[1].volume is None
    assert items[0].rank == 1


async def test_trends24_count_cap_and_failures():
    sess = _Session({"trends24": _Resp(text=TRENDS24_HTML)})
    assert len(await _from_trends24(sess, "u", 1)) == 1
    assert await _from_trends24(_Session({"trends24": _Resp(status=503)}), "u", 5) == []
    assert await _from_trends24(_Session({"trends24": RuntimeError("net")}), "u", 5) == []
    assert await _from_trends24(_Session({"trends24": _Resp(text="<html></html>")}), "u", 5) == []


async def test_getdaytrends_parses_table():
    sess = _Session({"getdaytrends": _Resp(text=GETDAYTRENDS_HTML)})
    items = await _from_getdaytrends(sess, "united-states", 10)
    assert [i.tag for i in items] == ["#alpha", "#beta"]


async def test_fetch_x_trends_chain(monkeypatch):
    async def t24_empty(session, region, count):
        return []

    class _Item:
        rank, tag, url, volume = 1, "#g", None, None

    async def gdt(session, region, count):
        return [_Item()]

    monkeypatch.setattr("ujin.sources.social.x_trends._from_trends24", t24_empty)
    monkeypatch.setattr("ujin.sources.social.x_trends._from_getdaytrends", gdt)
    result = await fetch_x_trends("united-states/", 5)
    assert result.source == "getdaytrends"
    assert result.region == "united-states"

    async def gdt_empty(session, region, count):
        return []

    monkeypatch.setattr("ujin.sources.social.x_trends._from_getdaytrends", gdt_empty)
    result = await fetch_x_trends("united-states", 5)
    assert result.source == "empty" and result.items == []


# ── SitePollable ─────────────────────────────────────────────────────────────

PAGE_V1 = """<html><body>
<main><h2>headline one</h2></main>
<aside>ad rotation 123</aside>
</body></html>"""
PAGE_V2_COSMETIC = PAGE_V1.replace("ad rotation 123", "ad rotation 999")
PAGE_V2_REAL = PAGE_V1.replace("headline one", "headline two")


async def test_site_pollable_scoped_change_detection(fake_origin):
    route = fake_origin.add("/page", body=PAGE_V1)
    async with HttpFetcher() as http:
        p = SitePollable(fake_origin.url("/page"), ["main"], fetcher=http)
        first = await p.poll(None)
        assert first.ok and first.changed

        route.body = PAGE_V2_COSMETIC   # aside churn only
        second = await p.poll(first)
        assert second.changed is False  # selector scoping ignored it

        route.body = PAGE_V2_REAL
        third = await p.poll(second)
        assert third.changed is True
        diff = third.payload["region_diff"]
        assert diff is not None


async def test_site_pollable_no_selectors_whole_page(fake_origin):
    route = fake_origin.add("/page", body=PAGE_V1)
    async with HttpFetcher() as http:
        p = SitePollable(fake_origin.url("/page"), fetcher=http)
        first = await p.poll(None)
        route.body = PAGE_V2_COSMETIC
        second = await p.poll(first)
        assert second.changed is True   # whole-page parity with HttpPollable


async def test_site_pollable_304_keeps_regions(fake_origin):
    fake_origin.add("/page", body=PAGE_V1, etag='"v1"')
    async with HttpFetcher() as http:
        p = SitePollable(fake_origin.url("/page"), ["main"], fetcher=http)
        first = await p.poll(None)
        second = await p.poll(first)
        assert second.status == 304
        assert second.changed is False
        assert second.payload["regions"] == first.payload["regions"]


async def test_site_pollable_5xx_and_connection_error(fake_origin):
    fake_origin.add("/down", body="x", status=503)
    async with HttpFetcher() as http:
        p = SitePollable(fake_origin.url("/down"), ["main"], fetcher=http)
        r = await p.poll(None)
        assert r.ok is False and r.status == 503

    p2 = SitePollable("http://127.0.0.1:1/", ["main"])
    r2 = await p2.poll(None)
    assert r2.ok is False and r2.error
    if p2._fetcher is not None:
        await p2._fetcher.close()
