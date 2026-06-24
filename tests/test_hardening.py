"""Offline coverage-hardening tests.

Closes remaining line/branch gaps in:
  * ujin/robots.py        — 55->57, 75->37, 131, 144-145, 161-171
  * ujin/scrape/build.py  — 98-102, 125-128
  * ujin/extract/images.py  — 43-44, 52-53, 61-62, 115-116, 126
  * ujin/extract/contacts.py — 85-86, 94-95, 123-124, 153, 158->exit, 162->exit

All tests are offline and deterministic.
"""
from __future__ import annotations

import sys

import pytest

from ujin.extract.contacts import extract_contacts
from ujin.extract.images import extract_images
from ujin.robots import RobotsPolicy


# ══════════════════════════════════════════════════════════════ robots.py ══


# line 55->57: agent already in _rules from an earlier group; the rule-list
# init (line 56) is skipped and we go straight to current_agents.append.
def test_same_agent_in_two_groups_accumulates_rules():
    txt = (
        "User-agent: Googlebot\nDisallow: /alpha\n\n"
        "User-agent: Googlebot\nDisallow: /beta\n"
    )
    p = RobotsPolicy(txt)
    assert p.is_allowed("/alpha", agent="Googlebot") is False
    assert p.is_allowed("/beta", agent="Googlebot") is False
    assert p.is_allowed("/other", agent="Googlebot") is True


# line 75->37: key is not user-agent/allow/disallow/crawl-delay/sitemap;
# all elif branches evaluate to False and control returns to the for-loop.
def test_unknown_directive_key_silently_ignored():
    txt = "User-agent: *\nDisallow: /secret\nX-Custom-Directive: value\n"
    p = RobotsPolicy(txt)
    assert p.is_allowed("/secret") is False
    assert p.is_allowed("/public") is True


# line 131: _match("", path) → (True, 0). Reached by calling the static
# method directly; an empty Disallow is filtered upstream but Allow: is not.
def test_match_empty_pattern_returns_true_length_zero():
    matched, length = RobotsPolicy._match("", "/anything/at/all")
    assert matched is True
    assert length == 0


# lines 144-145: re.error inside _match is caught; (False, 0) is returned.
def test_match_regex_error_caught_returns_false(monkeypatch):
    import re as _re
    import ujin.robots as _robots_mod

    def _raising_match(pattern, string):
        raise _re.error("simulated error")

    monkeypatch.setattr(_robots_mod.re, "match", _raising_match)
    result = RobotsPolicy._match("/foo", "/foo")
    assert result == (False, 0)


# lines 161-168: _http_fetcher — 200 success path.
@pytest.mark.asyncio
async def test_http_fetcher_200_returns_body_text(monkeypatch):
    import aiohttp
    from ujin.robots import _http_fetcher

    _BODY = "User-agent: *\nDisallow: /\n"

    class _FakeResp:
        status = 200

        async def text(self, errors=None):
            return _BODY

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeSession:
        def __init__(self, timeout=None):
            pass

        def get(self, url):
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    result = await _http_fetcher("http://example.com/robots.txt")
    assert result == _BODY


# lines 169-171: _http_fetcher — any exception returns "".
@pytest.mark.asyncio
async def test_http_fetcher_connection_error_returns_empty(monkeypatch):
    import aiohttp
    from ujin.robots import _http_fetcher

    class _FailSession:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            raise ConnectionError("network unreachable")

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", _FailSession)
    result = await _http_fetcher("http://unreachable.example/robots.txt")
    assert result == ""


# lines 167, 171: non-200 response returns "".
@pytest.mark.asyncio
async def test_http_fetcher_non_200_returns_empty(monkeypatch):
    import aiohttp
    from ujin.robots import _http_fetcher

    class _FakeResp:
        status = 404

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeSession:
        def __init__(self, timeout=None):
            pass

        def get(self, url):
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    result = await _http_fetcher("http://example.com/robots.txt")
    assert result == ""


# ══════════════════════════════════════════════════ ujin/scrape/build.py ══


# lines 98-102: learn_strategy=True → StrategyFeedback is imported and
# instantiated inside build_scrape_components.
@pytest.mark.asyncio
async def test_build_components_learn_strategy_creates_strategy(monkeypatch):
    from ujin.scrape.build import build_scrape_components
    from ujin.scrape.config import ScrapeConfig

    class _FakeStrategy:
        def __init__(self, store):
            self.store = store

        def close(self):
            pass

    monkeypatch.setattr("ujin.adapt.strategy.StrategyFeedback", _FakeStrategy)
    cfg = ScrapeConfig(learn_strategy=True, strategy_db=":memory:")
    comps = await build_scrape_components(cfg)
    assert isinstance(comps.strategy, _FakeStrategy)
    assert comps.strategy.store == ":memory:"
    await comps.http.close()


# lines 124-126: close_scrape_components calls strategy.close() when present.
@pytest.mark.asyncio
async def test_close_components_strategy_close_called():
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.build import ScrapeComponents, close_scrape_components
    from ujin.scrape.host_overrides import HostOverrideRegistry
    from ujin.scrape.metrics import HostMetrics

    closed = []

    class _FakeHttp:
        async def close(self):
            pass

    class _FakeStrategy:
        def close(self):
            closed.append(True)

    comps = ScrapeComponents(
        http=_FakeHttp(),
        obscura=object(),
        cache=ScrapeCache(),
        policy=HostPolicy(),
        metrics=HostMetrics(),
        overrides=HostOverrideRegistry(),
        disk=None,
        strategy=_FakeStrategy(),
    )
    await close_scrape_components(comps)
    assert closed == [True]


# lines 127-128: strategy.close() raising is caught and logged, not re-raised.
@pytest.mark.asyncio
async def test_close_components_strategy_close_exception_swallowed():
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.build import ScrapeComponents, close_scrape_components
    from ujin.scrape.host_overrides import HostOverrideRegistry
    from ujin.scrape.metrics import HostMetrics

    class _FakeHttp:
        async def close(self):
            pass

    class _BadStrategy:
        def close(self):
            raise RuntimeError("strategy close failed")

    comps = ScrapeComponents(
        http=_FakeHttp(),
        obscura=object(),
        cache=ScrapeCache(),
        policy=HostPolicy(),
        metrics=HostMetrics(),
        overrides=HostOverrideRegistry(),
        disk=None,
        strategy=_BadStrategy(),
    )
    await close_scrape_components(comps)  # must not raise


# ════════════════════════════════════════════ ujin/extract/images.py ══


# line 126: _first_srcset returns None when the first comma-delimited segment
# is empty (srcset starts with a comma).
def test_images_srcset_leading_comma_yields_no_candidate():
    out = extract_images('<img srcset=",800w" alt="x">')
    assert out == []


# lines 43-44: selectolax import fails → extract_images returns [].
def test_images_selectolax_import_failure_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "selectolax", None)
    monkeypatch.setitem(sys.modules, "selectolax.parser", None)
    out = extract_images('<img src="/a.jpg">')
    assert out == []


# lines 52-53: _image_record raises for one node; that node is skipped and
# the inner except continues to the next img.
def test_images_bad_node_inner_except_skips_to_next(monkeypatch):
    import ujin.extract.images as _m

    _orig = _m._image_record
    _n = [0]

    def _patched(node, base_url):
        _n[0] += 1
        if _n[0] == 1:
            raise ValueError("simulated bad node")
        return _orig(node, base_url)

    monkeypatch.setattr(_m, "_image_record", _patched)
    out = extract_images(
        '<img src="/a.jpg"><img src="/b.jpg">',
        base_url="https://x.test/",
    )
    assert len(out) == 1
    assert out[0]["src"] == "https://x.test/b.jpg"


# lines 61-62: tree.css() raises → outer except returns whatever was
# accumulated so far (empty list here since no nodes were yielded).
def test_images_css_raises_outer_except_returns_partial(monkeypatch):
    import selectolax.parser as _sl

    class _BadParser:
        def __init__(self, html):
            pass

        def css(self, selector):
            raise RuntimeError("parser exploded")

    monkeypatch.setattr(_sl, "HTMLParser", _BadParser)
    out = extract_images('<img src="/a.jpg">')
    assert out == []


# lines 115-116: urljoin raises in _resolve_src → raw src is returned.
def test_images_urljoin_exception_falls_back_to_raw_src(monkeypatch):
    import ujin.extract.images as _m

    def _bad_urljoin(*a, **kw):
        raise ValueError("bad join")

    monkeypatch.setattr(_m, "urljoin", _bad_urljoin)
    out = extract_images('<img src="/a.jpg">', base_url="https://x.test/")
    assert out == [{"src": "/a.jpg", "alt": ""}]


# ══════════════════════════════════════════ ujin/extract/contacts.py ══


# line 153: handle_starttag returns early when href is empty or absent.
def test_contacts_empty_href_anchor_ignored():
    html = '<a href="">Empty href</a><a href="mailto:alice@example.com">Email</a>'
    result = extract_contacts(html)
    assert result["emails"] == ["alice@example.com"]


def test_contacts_missing_href_anchor_ignored():
    html = '<a>No href at all</a><a href="tel:+15551234567">Call</a>'
    result = extract_contacts(html)
    assert result["phones"] == ["+15551234567"]


# line 158->exit: mailto: with empty email (query-only address); `if email:`
# is False so the email is not appended.
def test_contacts_mailto_empty_address_not_collected():
    html = '<a href="mailto:?subject=Hello">Contact</a>'
    result = extract_contacts(html)
    assert result["emails"] == []


# line 162->exit: tel: with no digits; `if phone:` is False so not appended.
def test_contacts_tel_empty_number_not_collected():
    html = '<a href="tel:">Call</a>'
    result = extract_contacts(html)
    assert result["phones"] == []


# lines 85-86: urlparse raises inside _is_social_href → except returns False.
def test_contacts_is_social_href_urlparse_exception_returns_false(monkeypatch):
    import ujin.extract.contacts as _m

    def _bad_urlparse(href):
        raise ValueError("bad url")

    monkeypatch.setattr(_m, "urlparse", _bad_urlparse)
    html = '<a href="https://twitter.com/example">Twitter</a>'
    result = extract_contacts(html)
    # urlparse raises → _is_social_href returns False → link not collected
    assert result["links"] == []


# lines 94-95: urljoin raises inside _resolve → raw value is returned.
def test_contacts_resolve_urljoin_exception_returns_raw(monkeypatch):
    import ujin.extract.contacts as _m

    def _bad_urljoin(*a, **kw):
        raise ValueError("bad join")

    monkeypatch.setattr(_m, "urljoin", _bad_urljoin)
    # rel="me" short-circuits _is_social_href before urlparse; _resolve then
    # tries urljoin which raises, so the raw href is returned unchanged.
    html = '<a rel="me" href="/profiles/user">Profile</a>'
    result = extract_contacts(html, base_url="https://example.com/")
    assert "/profiles/user" in result["links"]


# lines 123-124: parser.feed() raises → except Exception: pass (no raise).
def test_contacts_parser_feed_exception_swallowed(monkeypatch):
    import ujin.extract.contacts as _m

    class _BadParser:
        def __init__(self, base_url=None):
            self.emails: list[str] = []
            self.phones: list[str] = []
            self.links: list[str] = []

        def feed(self, data):
            raise Exception("parser exploded")

        def close(self):
            pass

    monkeypatch.setattr(_m, "_ContactsParser", _BadParser)
    result = extract_contacts("<html><body>hello</body></html>")
    assert result == {"emails": [], "phones": [], "links": []}
