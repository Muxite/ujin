"""Shared test infrastructure.

Everything here is offline and deterministic:

- ``fake_clock`` — manual time + instant async sleep, for adaptive/backoff logic.
- ``FakePage`` / ``fake_page`` — in-memory implementation of the ``_Page``
  protocol from ``ujin.fetch.browser``; drives recipe actions without a browser.
- ``fake_origin`` — a real aiohttp server on localhost with programmable routes
  (status, body, headers, delay, ETag/304), for ``HttpFetcher``/scrape tests.
- ``obscura_stub_bin`` — a tiny Python script standing in for the obscura
  binary (prints canned HTML to stdout); set via ``OBSCURA_BIN``.
- ``html_corpus`` — saved pages under ``tests/fixtures/html/`` shared with
  the extract tests and the benchmark suite.
- ``browser`` / ``obscura`` marked tests auto-skip unless the real backend is
  present (never in CI).
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Marker auto-skip: `browser` needs playwright/selenium importable, `obscura`
# needs a real binary. CI never has either; locally they run when present.
# --------------------------------------------------------------------------- #
def pytest_collection_modifyitems(config, items):
    try:
        from ujin.fetch.browser import browser_available
        have_browser = browser_available("playwright") or browser_available("selenium")
    except Exception:
        have_browser = False
    try:
        from ujin.fetch.obscura import obscura_available
        # The env-based check can be fooled by our own stubs; only trust it
        # when the caller hasn't overridden OBSCURA_BIN/OBSCURA_URL.
        have_obscura = (
            "OBSCURA_BIN" not in os.environ
            and "OBSCURA_URL" not in os.environ
            and obscura_available()
        )
    except Exception:
        have_obscura = False

    skip_browser = pytest.mark.skip(reason="no real browser backend installed")
    skip_obscura = pytest.mark.skip(reason="obscura binary not built")
    for item in items:
        if "browser" in item.keywords and not have_browser:
            item.add_marker(skip_browser)
        if "obscura" in item.keywords and not have_obscura:
            item.add_marker(skip_obscura)


# --------------------------------------------------------------------------- #
# Fake clock
# --------------------------------------------------------------------------- #
class FakeClock:
    """Manual time; ``sleep`` advances instantly. Compatible both with the
    engine's injected ``time_fn`` and the browser ``_ActionCtx`` knobs."""

    def __init__(self, start: float = 0.0):
        self.t = float(start)

    def now(self) -> float:
        return self.t

    # engine-style callable
    def __call__(self) -> float:
        return self.t

    def advance(self, d: float) -> None:
        self.t += d

    async def sleep(self, d: float) -> None:
        self.t += max(0.0, d)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


# --------------------------------------------------------------------------- #
# FakePage — full _Page protocol, scriptable DOM state.
# --------------------------------------------------------------------------- #
@dataclass
class FakePage:
    """In-memory ``_Page``. Configure ``html``, ``selectors`` (selector ->
    count), ``enabled``/``present`` button state, and load-more growth.

    Every protocol method is implemented so any recipe action can run.
    Interactions are recorded in ``log`` for assertions.
    """

    html: str = "<html><body></body></html>"
    url: str = "about:blank"
    selectors: dict[str, int] = field(default_factory=dict)
    present: dict[str, bool] = field(default_factory=dict)
    enabled: dict[str, bool] = field(default_factory=dict)
    harvest_items: dict[str, list[dict]] = field(default_factory=dict)
    eval_results: dict[str, Any] = field(default_factory=dict)
    # load-more simulation
    per_click: int = 0
    total_clicks: int = 0
    count_cap: Optional[int] = None
    grow_selector: Optional[str] = None
    click_raises: bool = False
    fail_goto: Optional[Exception] = None
    wait_raises: Optional[Exception] = None

    def __post_init__(self):
        self.log: list[tuple] = []
        self.closed = False
        self._remaining = self.total_clicks

    # -- navigation / snapshot ------------------------------------------------
    async def goto(self, url: str) -> None:
        self.log.append(("goto", url))
        if self.fail_goto is not None:
            raise self.fail_goto
        self.url = url

    async def content(self) -> str:
        return self.html

    async def final_url(self) -> str:
        return self.url

    # -- interaction ----------------------------------------------------------
    def _grow(self) -> None:
        if self._remaining > 0 and self.grow_selector:
            self._remaining -= 1
            n = self.selectors.get(self.grow_selector, 0) + self.per_click
            if self.count_cap is not None:
                n = min(n, self.count_cap)
            self.selectors[self.grow_selector] = n

    async def click(self, selector: str) -> None:
        self.log.append(("click", selector))
        if self.click_raises:
            raise RuntimeError("element detached")
        self._grow()

    async def fill(self, selector: str, value: str) -> None:
        self.log.append(("fill", selector, value))

    async def press(self, selector: str, key: str) -> None:
        self.log.append(("press", selector, key))

    async def query_count(self, selector: str) -> int:
        return self.selectors.get(selector, 0)

    async def exists(self, selector: str) -> bool:
        if selector in self.present:
            if self.grow_selector and self._remaining <= 0:
                return False
            return self.present[selector]
        return self.selectors.get(selector, 0) > 0

    async def is_enabled(self, selector: str) -> bool:
        return self.enabled.get(selector, True)

    async def wait_for_selector(self, selector: str, timeout_ms: int) -> None:
        self.log.append(("wait_for_selector", selector, timeout_ms))
        if self.wait_raises is not None:
            raise self.wait_raises

    async def scroll_into_view(self, selector: str) -> None:
        self.log.append(("scroll_into_view", selector))

    async def scroll_to_bottom(self) -> None:
        self.log.append(("scroll_to_bottom",))
        self._grow()

    async def eval_js(self, script: str) -> Any:
        self.log.append(("eval_js", script))
        return self.eval_results.get(script)

    async def screenshot(self) -> bytes:
        return b"\x89PNGfake"

    async def harvest(self, selector: str) -> list[dict]:
        return list(self.harvest_items.get(selector, []))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_page() -> FakePage:
    return FakePage()


# --------------------------------------------------------------------------- #
# Fake origin — a real aiohttp server with programmable routes.
# --------------------------------------------------------------------------- #
@dataclass
class Route:
    body: str | bytes = ""
    status: int = 200
    content_type: str = "text/html"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    etag: Optional[str] = None


class FakeOrigin:
    """Programmable localhost origin.

    ``origin.add("/page", body="<html>...", status=200, delay=0.1)`` then fetch
    ``origin.url("/page")``. Requests are recorded in ``origin.requests``.
    Routes with an ``etag`` answer 304 to matching ``If-None-Match``.
    """

    def __init__(self):
        from aiohttp import web
        self._web = web
        self.routes: dict[str, Route] = {}
        self.requests: list[Any] = []
        self.inflight = 0
        self.max_inflight = 0
        self._server = None
        self._app = web.Application()
        self._app.router.add_route("*", "/{tail:.*}", self._handle)

    def add(self, path: str, **kw) -> Route:
        r = Route(**kw)
        self.routes[path] = r
        return r

    async def _handle(self, request):
        self.requests.append(request)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            route = self.routes.get(request.path)
            if route is None:
                return self._web.Response(status=404, text="not found")
            if route.delay:
                await asyncio.sleep(route.delay)
            headers = dict(route.headers)
            if route.etag:
                headers["ETag"] = route.etag
                if request.headers.get("If-None-Match") == route.etag:
                    return self._web.Response(status=304, headers=headers)
            body = route.body.encode() if isinstance(route.body, str) else route.body
            return self._web.Response(
                body=body, status=route.status,
                content_type=route.content_type, headers=headers,
            )
        finally:
            self.inflight -= 1

    async def start(self):
        from aiohttp.test_utils import TestServer
        self._server = TestServer(self._app)
        await self._server.start_server()
        return self

    async def stop(self):
        if self._server is not None:
            await self._server.close()

    def url(self, path: str = "/") -> str:
        assert self._server is not None, "origin not started"
        return str(self._server.make_url(path))

    @property
    def host(self) -> str:
        return f"{self._server.host}:{self._server.port}"


@pytest.fixture
async def fake_origin():
    origin = FakeOrigin()
    await origin.start()
    yield origin
    await origin.stop()


# --------------------------------------------------------------------------- #
# Obscura stub binary — a python script that mimics `obscura fetch URL --dump html`.
# --------------------------------------------------------------------------- #
OBSCURA_STUB = textwrap.dedent(
    """\
    #!{python}
    import sys, time
    args = sys.argv[1:]
    mode = "{mode}"
    if mode == "hang":
        time.sleep(3600)
    if mode == "fail":
        sys.exit(3)
    url = args[1] if len(args) > 1 else ""
    sys.stdout.write("<html><body><h1>rendered: " + url + "</h1>"
                     + "<a href='/r/1'>one</a>" * 6 + "</body></html>")
    """
)


def make_obscura_stub(tmp_path: Path, mode: str = "ok") -> str:
    """Write an executable stub obscura binary; return its path."""
    script = tmp_path / f"obscura-stub-{mode}"
    script.write_text(OBSCURA_STUB.format(python=sys.executable, mode=mode))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def obscura_stub_bin(tmp_path, monkeypatch) -> str:
    """A working stub obscura binary, exported as OBSCURA_BIN."""
    path = make_obscura_stub(tmp_path, "ok")
    monkeypatch.setenv("OBSCURA_BIN", path)
    monkeypatch.delenv("OBSCURA_URL", raising=False)
    return path


# --------------------------------------------------------------------------- #
# Duck-typed service-layer fakes (shared by scrape-service and routes tests).
# --------------------------------------------------------------------------- #
class FakeHttp:
    """Routes GETs by URL; mimics HttpFetcher.get. With ``not_modified=True``
    answers 304 to any conditional request carrying an ETag."""

    def __init__(self, routes: Optional[dict] = None, *, not_modified: bool = False):
        self._routes = routes or {}
        self._not_modified = not_modified
        self.calls: list[str] = []

    async def get(self, url, *, etag=None, last_modified=None,
                  extra_headers=None, proxy=None):
        from ujin.fetch.http import HttpResponse

        self.calls.append(url)
        if self._not_modified and etag is not None:
            return HttpResponse(url=url, status=304, body="",
                                not_modified=True, final_url=url)
        resp = self._routes.get(url)
        if resp is None:
            return HttpResponse(url=url, status=404, body="", final_url=url)
        if isinstance(resp, Exception):
            raise resp
        return resp


class FakeObscura:
    """Canned obscura renderer; ``html=None`` simulates unavailability."""

    def __init__(self, html: Optional[str] = None):
        self._html = html
        self.calls: list[str] = []

    async def render_html(self, url):
        from ujin.fetch.obscura import ObscuraResult

        self.calls.append(url)
        if self._html is None:
            raise RuntimeError("obscura unavailable")
        return ObscuraResult(url=url, html=self._html, elapsed_ms=1)


class FakeBrowser:
    """Duck-typed BrowserFetcher returning canned HTML for any recipe."""

    def __init__(self, html: str = "<html></html>", *, raises: Optional[Exception] = None):
        self.html = html
        self.raises = raises
        self.calls: list[tuple[str, list]] = []

    async def render(self, url, actions=None, *, results_selector=None, ctx=None):
        from ujin.fetch.browser import BrowserResult

        self.calls.append((url, actions or []))
        if self.raises is not None:
            raise self.raises
        return BrowserResult(url=url, html=self.html, elapsed_ms=2, final_url=url)


# --------------------------------------------------------------------------- #
# HTML corpus
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def html_corpus() -> dict[str, str]:
    """All saved pages under tests/fixtures/html keyed by stem."""
    corpus_dir = FIXTURES / "html"
    return {p.stem: p.read_text() for p in sorted(corpus_dir.glob("*.html"))}
