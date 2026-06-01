"""Sessions, proxy pool, and structured-extraction tests (pure-python)."""
from __future__ import annotations

from ujin.extract.structured import extract_structured
from ujin.proxy.pool import ProxyPool


def test_proxy_pool_round_robin_and_health():
    pool = ProxyPool(["http://a", "http://b", "http://c"], cooldown_secs=60)
    # Round-robin order.
    assert [pool.acquire() for _ in range(3)] == ["http://a", "http://b", "http://c"]
    # Bench one; it should be skipped.
    pool.record_failure("http://b")
    got = {pool.acquire() for _ in range(6)}
    assert "http://b" not in got
    assert got == {"http://a", "http://c"}
    # Recover it.
    pool.record_success("http://b")
    assert "http://b" in pool.healthy()


def test_proxy_pool_empty():
    pool = ProxyPool([])
    assert not pool
    assert pool.acquire() is None


_HTML = """
<html><head>
  <meta property="og:title" content="A Great Article">
  <meta name="description" content="A summary">
  <script type="application/ld+json">
  {"@type": "NewsArticle", "headline": "A Great Article", "author": "Jane"}
  </script>
</head>
<body itemscope itemtype="https://schema.org/Article">
  <h1 itemprop="headline">A Great Article</h1>
  <span itemprop="author">Jane</span>
</body></html>
"""


def test_extract_structured_jsonld_og_microdata():
    data = extract_structured(_HTML)
    assert any(d.get("@type") == "NewsArticle" for d in data["jsonld"])
    assert data["opengraph"]["og:title"] == "A Great Article"
    assert data["opengraph"]["description"] == "A summary"
    assert data["microdata"]
    assert data["microdata"][0]["props"]["headline"] == "A Great Article"


def test_structured_jsonld_graph_unwrap():
    html = """<script type="application/ld+json">
    {"@graph": [{"@type": "Org"}, {"@type": "WebSite"}]}
    </script>"""
    data = extract_structured(html)
    types = {d.get("@type") for d in data["jsonld"]}
    assert types == {"Org", "WebSite"}


async def test_session_store_in_memory():
    """SessionStore exposes an aiohttp CookieJar even without a path.

    Async because aiohttp's CookieJar binds to the running event loop at
    construction — which is exactly how SessionStore is used (before the
    async HttpFetcher.start()).
    """
    from ujin.session import SessionStore

    store = SessionStore()
    assert store.jar is not None
    store.save()  # no path -> no-op, must not raise
    store.clear()
