"""ScrapeService fallback-chain branches not covered by test_scrape_service.py:
combined fan-out, batch partial failures, article mode, render pinning,
override-driven direct sitemap/RSS, link filters, and enrichment.
"""
from __future__ import annotations

import pytest

from conftest import FakeBrowser, FakeHttp, FakeObscura
from ujin.cache import HostPolicy, ScrapeCache
from ujin.fetch.http import HttpResponse
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.service import ScrapeService


def _resp(url, body, status=200, **kw):
    return HttpResponse(url=url, status=status, body=body, final_url=url, **kw)


def _service(http, **kw):
    kw.setdefault("obscura", FakeObscura())
    kw.setdefault("cache", ScrapeCache())
    kw.setdefault("policy", HostPolicy(cooldown_secs=60))
    kw.setdefault("config", ScrapeConfig())
    return ScrapeService(http=http, **kw)


def _index_html(n=8, host="https://news.example.com"):
    rows = "".join(
        f'<article><h2><a href="{host}/2026/06/09/story-{i}">'
        f"A sufficiently long headline number {i}</a></h2></article>"
        for i in range(n)
    )
    return f"<html><body><main>{rows}</main></body></html>"


HOME = "https://news.example.com/"


# ── article mode ─────────────────────────────────────────────────────────────

async def test_article_mode_extracts_and_caches(html_corpus):
    url = f"{HOME}2026/06/08/quantum-error-correction-milestone"
    http = FakeHttp({url: _resp(url, html_corpus["article"])})
    svc = _service(http)
    r = await svc.scrape(url, mode="article")
    assert r.kind == "article"
    assert r.article is not None and "logical qubit" in r.article.text
    assert r.fingerprint  # sha256 of article text
    assert r.cached is False


async def test_article_mode_empty_when_unextractable():
    url = f"{HOME}x"
    http = FakeHttp({url: _resp(url, "<html><body></body></html>")})
    r = await _service(http).scrape(url, mode="article")
    assert r.kind == "empty"
    assert r.article is None
    assert r.fingerprint == ""


# ── render pinning ───────────────────────────────────────────────────────────

async def test_render_http_pinned_never_escalates():
    """render='http' on a thin page must NOT fall through to obscura, and
    (0.4.0) the thin link-set is still returned rather than failing."""
    obscura = FakeObscura(html=_index_html())
    thin = ('<html><body><main><article><h2><a href="/2026/06/09/only-one">'
            "A single sufficiently long headline</a></h2></article>"
            "</main></body></html>")
    http = FakeHttp({HOME: _resp(HOME, thin)})  # 1 link < fast_path_min_links
    svc = _service(http, obscura=obscura)
    r = await svc.scrape(HOME, mode="links", render="http")
    assert obscura.calls == []
    assert r.strategy_used == "http"
    assert len(r.links) == 1


async def test_render_obscura_pinned_skips_http():
    obscura = FakeObscura(html=_index_html())
    http = FakeHttp({HOME: _resp(HOME, _index_html())})
    svc = _service(http, obscura=obscura)
    r = await svc.scrape(HOME, mode="links", render="obscura")
    assert http.calls == []          # straight to the renderer
    assert obscura.calls == [HOME]
    assert r.strategy_used == "obscura"
    assert len(r.links) == 8


async def test_render_browser_without_fetcher_falls_to_altpath_or_fails():
    http = FakeHttp({})
    svc = _service(http, browser=None)
    with pytest.raises(RuntimeError):
        await svc.scrape(HOME, mode="article", render="browser")


async def test_render_browser_error_recovers_to_failure():
    browser = FakeBrowser(raises=RuntimeError("crashed"))
    svc = _service(FakeHttp({}), browser=browser)
    with pytest.raises(RuntimeError):
        await svc.scrape(HOME, mode="article", render="browser")
    assert browser.calls  # it was attempted


# ── obscura escalation on thin HTTP results ──────────────────────────────────

async def test_thin_http_escalates_to_obscura_which_wins():
    obscura = FakeObscura(html=_index_html())
    http = FakeHttp({HOME: _resp(HOME, "<html><a href='/one'>just one</a></html>")})
    svc = _service(http, obscura=obscura)
    r = await svc.scrape(HOME, mode="links")
    assert r.strategy_used == "obscura"
    assert r.used_renderer is True
    assert len(r.links) == 8


async def test_rich_http_skips_obscura():
    obscura = FakeObscura(html="<html></html>")
    http = FakeHttp({HOME: _resp(HOME, _index_html())})
    svc = _service(http, obscura=obscura)
    r = await svc.scrape(HOME, mode="links")
    assert r.strategy_used == "http"
    assert obscura.calls == []


# ── batch ────────────────────────────────────────────────────────────────────

async def test_batch_partial_failures_inline():
    ok = f"{HOME}"
    bad = "https://dead.example.org/"
    http = FakeHttp({ok: _resp(ok, _index_html())})
    svc = _service(http, obscura=FakeObscura(html=None))
    results = await svc.scrape_batch([(ok, "links", False), (bad, "links", False)])
    assert len(results) == 2
    assert results[0].kind == "links"
    assert isinstance(results[1], Exception)


async def test_batch_preserves_order():
    urls = [f"{HOME}p{i}" for i in range(4)]
    routes = {u: _resp(u, _index_html(host=u.rstrip("/"))) for u in urls}
    svc = _service(FakeHttp(routes))
    results = await svc.scrape_batch([(u, "links", False) for u in urls])
    assert [r.url for r in results] == urls


# ── overrides: direct sitemap / rss strategies ───────────────────────────────

SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://news.example.com/2026/06/09/wire-a</loc></url>
  <url><loc>https://news.example.com/sports/lottery-numbers</loc></url>
  <url><loc>https://news.example.com/2026/06/09/wire-b</loc></url>
</urlset>"""


def _overrides(yaml_text, tmp_path):
    from ujin.scrape.host_overrides import HostOverrideRegistry

    p = tmp_path / "overrides.yaml"
    p.write_text(yaml_text)
    return HostOverrideRegistry.from_file(str(p))


async def test_override_pinned_sitemap_with_deny_filter(tmp_path):
    overrides = _overrides(
        """
hosts:
  news.example.com:
    strategy: sitemap_news
    sitemap_url: https://news.example.com/custom-sitemap.xml
    extract:
      url_path_deny_patterns: ["/sports/"]
""", tmp_path)
    sm = "https://news.example.com/custom-sitemap.xml"
    http = FakeHttp({sm: _resp(sm, SITEMAP)})
    svc = _service(http, overrides=overrides)
    r = await svc.scrape(HOME, mode="links")
    assert r.strategy_used == "sitemap_news"
    urls = [l.url for l in r.links]
    assert len(urls) == 2 and not any("/sports/" in u for u in urls)


async def test_override_pinned_rss(tmp_path):
    overrides = _overrides(
        """
hosts:
  news.example.com:
    strategy: rss
    rss_url: https://news.example.com/feed.xml
""", tmp_path)
    import ujin.scrape.service as svc_mod

    async def fake_parse(url):
        class _I:
            def __init__(self, u, t):
                self.url, self.title = u, t
                self.summary = ""
                self.published = ""
        return [_I("https://news.example.com/2026/06/09/rss-a", "From the feed A"),
                _I("https://news.example.com/2026/06/09/rss-b", "From the feed B")]

    svc = _service(FakeHttp({}), overrides=overrides)
    orig = svc_mod.parse_feed
    svc_mod.parse_feed = fake_parse
    try:
        r = await svc.scrape(HOME, mode="links")
    finally:
        svc_mod.parse_feed = orig
    assert r.strategy_used == "rss"
    assert len(r.links) == 2


# ── combined mode ────────────────────────────────────────────────────────────

def _patch_parse_feed(monkeypatch, fake):
    """_scrape_combined imports parse_feed from ujin.sources.rss at call time,
    while the altpath chain uses the binding in ujin.scrape.service — patch both."""
    monkeypatch.setattr("ujin.sources.rss.parse_feed", fake)
    monkeypatch.setattr("ujin.scrape.service.parse_feed", fake)


async def test_combined_merges_rss_and_html(tmp_path, monkeypatch):
    overrides = _overrides(
        """
hosts:
  news.example.com:
    rss_url: https://news.example.com/feed.xml
""", tmp_path)

    async def fake_parse(url):
        class _I:
            url = "https://news.example.com/2026/06/09/story-0"
            title = "A sufficiently long headline number 0"
            summary = "From the feed, with a summary."
            published = "2026-06-09"
        return [_I()]

    _patch_parse_feed(monkeypatch, fake_parse)
    http = FakeHttp({HOME: _resp(HOME, _index_html(n=6))})
    svc = _service(http, overrides=overrides)
    r = await svc.scrape(HOME, mode="combined")

    assert r.strategy_used == "combined"
    assert r.kind == "links"
    by_url = {l.url: l for l in r.links}
    merged = by_url["https://news.example.com/2026/06/09/story-0"]
    # the overlapping story carries RSS metadata and both provenance tags
    assert merged.summary == "From the feed, with a summary."
    assert set(merged.seen_in) == {"rss", "html"}
    # html-only stories are present too
    assert sum(1 for l in r.links if l.seen_in == ("html",)) == 5
    assert "rss:1" in r.note


async def test_combined_without_rss_degrades_to_html():
    http = FakeHttp({HOME: _resp(HOME, _index_html(n=6))})
    svc = _service(http)
    r = await svc.scrape(HOME, mode="combined")
    assert r.strategy_used == "combined"
    assert len(r.links) == 6
    assert all(l.seen_in == ("html",) for l in r.links)


async def test_combined_html_failure_reported_in_note(tmp_path, monkeypatch):
    """The HTML leg fails hard (host on cooldown, no cache) but the RSS leg
    still delivers, and the failure is surfaced in the note."""
    overrides = _overrides(
        """
hosts:
  dead.example.org:
    rss_url: https://dead.example.org/feed.xml
""", tmp_path)

    async def fake_parse(url):
        class _I:
            url = "https://dead.example.org/2026/06/09/only-rss"
            title = "Sufficiently long headline from rss"
            summary = ""
            published = ""
        return [_I()]

    _patch_parse_feed(monkeypatch, fake_parse)
    home = "https://dead.example.org/"
    policy = HostPolicy(cooldown_secs=60)
    policy.record_failure(home)  # inner mode=links scrape raises HostCooldown
    svc = _service(FakeHttp({}), obscura=FakeObscura(html=None),
                   overrides=overrides, policy=policy)
    r = await svc.scrape(home, mode="combined")
    # RSS leg still produced links even though the HTML leg failed
    assert len(r.links) == 1
    assert r.links[0].seen_in == ("rss",)
    assert "html_err=HostCooldown" in r.note


async def test_combined_enrich_attaches_article_summaries(html_corpus):
    """enrich_html_top_n fans out mode=article for html-only links and
    attaches the first long paragraph as the summary."""
    art_url = "https://news.example.com/2026/06/09/story-0"
    index = (
        "<html><body><main><article><h2>"
        f'<a href="{art_url}">A sufficiently long headline number 0</a>'
        "</h2></article>"
        + "".join(
            f'<article><h2><a href="https://news.example.com/2026/06/09/story-{i}">'
            f"A sufficiently long headline number {i}</a></h2></article>"
            for i in range(1, 6)
        )
        + "</main></body></html>"
    )
    http = FakeHttp({
        HOME: _resp(HOME, index),
        art_url: _resp(art_url, html_corpus["article"]),
    })
    svc = _service(http)
    r = await svc.scrape(HOME, mode="combined", enrich_html_top_n=1)
    enriched = [l for l in r.links if "article" in l.seen_in]
    assert len(enriched) == 1
    assert len(enriched[0].summary) >= 60
