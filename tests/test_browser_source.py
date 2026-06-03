"""browser source: registry build, BrowserPollable extraction, and the
load-more → chunk → sink path with a fake (no real browser) fetcher."""
from __future__ import annotations

from ujin.fetch.browser import BrowserResult
from ujin.jobs.pipeline import Pipeline
from ujin.jobs.transforms import build_transform
from ujin.poll.browser import BrowserPollable
from ujin.registry import register


class _FakeFetcher:
    """Returns canned HTML/items without launching a browser."""

    def __init__(self, *, html="<html></html>", items=None):
        self.html = html
        self.items = items
        self.calls = []

    async def render(self, url, actions=None, *, results_selector=None, ctx=None):
        self.calls.append({"url": url, "actions": actions,
                           "results_selector": results_selector})
        return BrowserResult(url=url, html=self.html, items=self.items,
                             elapsed_ms=12, final_url=url)


def test_registry_builds_browser_source():
    p = register.build_source("browser", {
        "url": "https://x.test", "extract": "links",
        "actions": [{"action": "load_more", "button": ".m", "results": ".i"}],
    })
    assert isinstance(p, BrowserPollable)
    assert p.url == "https://x.test" and p.extract == "links"
    assert p.actions[0]["action"] == "load_more"


async def test_browser_pollable_raw_items_payload():
    items = [{"text": f"pub {i}", "href": f"/p/{i}"} for i in range(5)]
    p = BrowserPollable("https://x.test", extract="raw",
                        results_selector=".publication-item",
                        fetcher=_FakeFetcher(items=items))
    r = await p.poll(None)
    assert r.ok and r.changed
    assert r.payload == items
    assert r.fingerprint


async def test_browser_pollable_links_extraction_runs():
    html = """<html><body>
      <a href="https://x.test/article/one">A reasonably long headline one</a>
      <a href="https://x.test/article/two">A reasonably long headline two</a>
    </body></html>"""
    p = BrowserPollable("https://x.test", extract="links",
                        fetcher=_FakeFetcher(html=html))
    r = await p.poll(None)
    assert r.ok and isinstance(r.payload, list)  # extractor ran on rendered HTML


async def test_load_more_then_chunk_to_sink():
    # the full "harvest everything, hand the LLM bites" path, no real browser
    items = [{"href": f"/p/{i}"} for i in range(7)]
    p = BrowserPollable("https://x.test", extract="raw",
                        results_selector=".item", fetcher=_FakeFetcher(items=items))
    result = await p.poll(None)

    seen = []

    class _Sink:
        async def emit(self, event):
            seen.append(event)

    pipe = Pipeline(transforms=[build_transform("chunk", {"size": 3})], sinks=[_Sink()])
    await pipe(p.key, result)
    # 7 items / size 3 -> 3 chunked events
    assert len(seen) == 3
    assert [len(e["payload"]) for e in seen] == [3, 3, 1]
    assert [e["chunk_index"] for e in seen] == [0, 1, 2]


async def test_browser_pollable_fetch_error_is_failure():
    class _Boom:
        async def render(self, *a, **k):
            raise RuntimeError("no browser")

    p = BrowserPollable("https://x.test", fetcher=_Boom())
    r = await p.poll(None)
    assert not r.ok and "no browser" in r.error
