"""BrowserFetcher recipe execution against FakePage — no real browser.

test_load_more.py covers the load_more/scroll growth loop; this file covers
the remaining primitives, the dispatch/error-recording path, registry-backed
custom actions, harvest, and screenshots.
"""
from __future__ import annotations

import pytest

from conftest import FakeClock, FakePage
from ujin.fetch.browser import (
    BrowserError,
    BrowserFetcher,
    _ActionCtx,
    _act_click,
    _act_eval_js,
    _act_fill,
    _act_goto,
    _act_press,
    _act_screenshot,
    _act_scroll,
    _act_wait_for_selector,
    _act_wait_ms,
    browser_available,
)


def _ctx(clock: FakeClock) -> _ActionCtx:
    return _ActionCtx(sleep=clock.sleep, clock=clock.now, default_timeout_ms=30000)


class _FakeBackend:
    """Stands in for _PlaywrightBackend; hands out a preconfigured FakePage."""

    def __init__(self, page: FakePage):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


def _fetcher(page: FakePage) -> tuple[BrowserFetcher, FakeClock]:
    clk = FakeClock()
    f = BrowserFetcher(sleep=clk.sleep, clock=clk.now)
    f._backend = _FakeBackend(page)  # bypass _ensure_backend
    return f, clk


# ── primitives ───────────────────────────────────────────────────────────────

async def test_primitives_drive_the_page():
    clk = FakeClock()
    page = FakePage(eval_results={"1+1": 2})
    ctx = _ctx(clk)

    assert (await _act_goto(ctx, page, url="https://x.test/"))["url"] == "https://x.test/"
    assert (await _act_click(ctx, page, selector=".b"))["selector"] == ".b"
    await _act_fill(ctx, page, selector="#q", value="hello")
    await _act_press(ctx, page, selector="#q", key="Enter")
    await _act_scroll(ctx, page)
    assert (await _act_eval_js(ctx, page, script="1+1"))["result"] == 2
    shot = await _act_screenshot(ctx, page, name="proof")
    assert shot["bytes"] > 0 and shot["_data"].startswith(b"\x89PNG")

    assert ("goto", "https://x.test/") in page.log
    assert ("fill", "#q", "hello") in page.log
    assert ("press", "#q", "Enter") in page.log
    assert ("scroll_to_bottom",) in page.log


async def test_wait_ms_uses_injected_sleep():
    clk = FakeClock()
    await _act_wait_ms(_ctx(clk), FakePage(), ms=1500)
    assert clk.t == pytest.approx(1.5)


async def test_wait_for_selector_default_timeout_applied():
    clk = FakeClock()
    page = FakePage()
    await _act_wait_for_selector(_ctx(clk), page, selector=".x")
    assert page.log[-1] == ("wait_for_selector", ".x", 30000)


# ── render: full recipe through the fetcher ─────────────────────────────────

async def test_render_runs_recipe_and_snapshots():
    page = FakePage(html="<html><body>final</body></html>",
                    harvest_items={".item": [{"t": "a"}, {"t": "b"}]})
    f, _ = _fetcher(page)
    result = await f.render(
        "https://x.test/",
        [{"action": "click", "selector": ".more"},
         {"action": "screenshot", "name": "after"}],
        results_selector=".item",
    )
    assert result.html == "<html><body>final</body></html>"
    assert result.final_url == "https://x.test/"
    assert [e["action"] for e in result.actions_log] == ["click", "screenshot"]
    assert all(e["ok"] for e in result.actions_log)
    assert result.items == [{"t": "a"}, {"t": "b"}]
    assert "after" in result.screenshots
    assert page.closed is True  # page always closed


async def test_render_records_action_errors_and_continues():
    """A failing step is logged ok=False; later steps still run and the
    partial HTML still comes back."""
    page = FakePage(click_raises=True)
    f, _ = _fetcher(page)
    result = await f.render(
        "https://x.test/",
        [{"action": "click", "selector": ".broken"},
         {"action": "wait_ms", "ms": 1}],
    )
    log = result.actions_log
    assert log[0]["ok"] is False and "error" in log[0]
    assert log[1]["ok"] is True
    assert result.html  # snapshot still taken


async def test_render_unknown_action_logged_not_raised():
    f, _ = _fetcher(FakePage())
    result = await f.render("https://x.test/", [{"action": "warp_drive"}])
    assert result.actions_log[0]["ok"] is False
    assert "unknown action" in result.actions_log[0]["error"]


async def test_render_custom_registry_action():
    from ujin import register

    @register.action("_probe_marker")
    def _factory(cfg, ctx):
        async def handler(page, **params):
            await page.click(params["selector"])
            return {"marked": True}
        return handler

    try:
        page = FakePage()
        f, _ = _fetcher(page)
        result = await f.render(
            "https://x.test/", [{"action": "_probe_marker", "selector": "#m"}]
        )
        assert result.actions_log[0]["ok"] is True
        assert result.actions_log[0]["marked"] is True
        assert ("click", "#m") in page.log
    finally:
        register.clear_plugins()


async def test_render_page_closed_even_when_goto_fails():
    page = FakePage(fail_goto=RuntimeError("dns error"))
    f, _ = _fetcher(page)
    with pytest.raises(RuntimeError, match="dns error"):
        await f.render("https://x.test/")
    assert page.closed is True


async def test_close_resets_backend():
    page = FakePage()
    f, _ = _fetcher(page)
    backend = f._backend
    await f.close()
    assert backend.closed is True
    assert f._backend is None


async def test_ensure_backend_unavailable_raises(monkeypatch):
    import ujin.fetch.browser as mod

    monkeypatch.setattr(mod, "browser_available", lambda engine: False)
    f = BrowserFetcher(engine="playwright")
    with pytest.raises(BrowserError, match="not installed"):
        await f._ensure_backend()


def test_browser_available_unknown_engine_maps_to_playwright():
    # contract: anything that isn't "selenium" checks the playwright package
    assert browser_available("playwright") == browser_available("whatever")
