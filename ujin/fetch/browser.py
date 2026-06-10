"""Browser automation fetcher — render a URL after running an interaction recipe.

Unlike :class:`ujin.fetch.obscura.ObscuraFetcher` (a *static* snapshot), this
fetcher drives a real browser through a declarative recipe — click, scroll, fill,
and crucially ``load_more`` (click a "load more" button until it runs out) — then
snapshots the fully-loaded HTML for the existing extractors.

Two interchangeable backends run the *same* recipe:
- **playwright** (default): native async.
- **selenium**: blocking WebDriver, marshalled onto a dedicated single thread
  (WebDriver is not thread-safe), so it cooperates with the event loop.

The module imports cleanly without either library installed; backends import
lazily and callers check :func:`browser_available` (mirroring ``obscura_available``)
so they degrade to HTTP/obscura when a browser isn't present.

All recipe primitives operate purely against the small :class:`_Page` protocol, so
the pagination logic (``load_more``/``scroll_to_bottom``) is unit-testable against
a fake page with an injected clock — no real browser needed.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from ujin.registry import BuildContext, register


class BrowserError(RuntimeError):
    pass


class BrowserTimeout(BrowserError):
    pass


@dataclass
class BrowserResult:
    url: str
    html: str
    items: Optional[list[dict]] = None       # harvested from results_selector
    actions_log: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0
    final_url: Optional[str] = None
    screenshots: Optional[dict[str, bytes]] = None


def browser_available(engine: str = "playwright") -> bool:
    """True when the backend library for ``engine`` is importable."""
    pkg = "selenium" if engine == "selenium" else "playwright"
    return importlib.util.find_spec(pkg) is not None


# --------------------------------------------------------------------------- #
# The page surface every backend exposes to recipe actions. A fake implementing
# these (sync or async) is enough to drive the pagination logic in tests.
# --------------------------------------------------------------------------- #
class _Page(Protocol):
    async def goto(self, url: str) -> None: ...
    async def content(self) -> str: ...
    async def final_url(self) -> str: ...
    async def click(self, selector: str) -> None: ...
    async def fill(self, selector: str, value: str) -> None: ...
    async def press(self, selector: str, key: str) -> None: ...
    async def query_count(self, selector: str) -> int: ...
    async def exists(self, selector: str) -> bool: ...
    async def is_enabled(self, selector: str) -> bool: ...
    async def wait_for_selector(self, selector: str, timeout_ms: int) -> None: ...
    async def scroll_into_view(self, selector: str) -> None: ...
    async def scroll_to_bottom(self) -> None: ...
    async def eval_js(self, script: str) -> Any: ...
    async def screenshot(self) -> bytes: ...
    async def harvest(self, selector: str) -> list[dict]: ...
    async def close(self) -> None: ...


@dataclass
class _ActionCtx:
    """Timing knobs injected into primitives (fake-able in tests)."""

    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    default_timeout_ms: int = 30000


# --------------------------------------------------------------------------- #
# Recipe primitives — pure functions of (_ActionCtx, _Page, **params).
# --------------------------------------------------------------------------- #
async def _act_goto(ctx: _ActionCtx, page: _Page, *, url: str, **_: Any) -> dict:
    await page.goto(url)
    return {"url": url}


async def _act_wait_for_selector(ctx, page, *, selector, timeout_ms=None, **_):
    await page.wait_for_selector(selector, timeout_ms or ctx.default_timeout_ms)
    return {"selector": selector}


async def _act_wait_ms(ctx, page, *, ms, **_):
    await ctx.sleep(ms / 1000)
    return {"ms": ms}


async def _act_click(ctx, page, *, selector, **_):
    await page.click(selector)
    return {"selector": selector}


async def _act_fill(ctx, page, *, selector, value, **_):
    await page.fill(selector, value)
    return {"selector": selector}


async def _act_press(ctx, page, *, selector, key, **_):
    await page.press(selector, key)
    return {"selector": selector, "key": key}


async def _act_eval_js(ctx, page, *, script, **_):
    return {"result": await page.eval_js(script)}


async def _act_screenshot(ctx, page, *, name="screenshot", **_):
    data = await page.screenshot()
    return {"name": name, "bytes": len(data) if data else 0, "_data": data}


async def _wait_for_growth(ctx: _ActionCtx, page: _Page, selector: str,
                           prev: int, settle_ms: int) -> bool:
    """Poll until the selector's count exceeds ``prev`` or a bounded wait lapses."""
    deadline = ctx.clock() + (settle_ms / 1000) * 4
    while ctx.clock() < deadline:
        if await page.query_count(selector) > prev:
            return True
        await ctx.sleep(settle_ms / 1000)
    return await page.query_count(selector) > prev


async def _grow_loop(ctx: _ActionCtx, page: _Page, *, button: Optional[str],
                     results: Optional[str], max_iters: int, timeout_ms: int,
                     settle_ms: int, cap_label: str = "max_iters") -> dict:
    """Shared engine for ``load_more`` (button) and ``scroll_to_bottom`` (no button).

    Repeatedly trigger more content; stop when the trigger is gone/disabled, when
    the results count stops growing, or when bounded by max_iters / timeout.
    """
    deadline = ctx.clock() + timeout_ms / 1000
    prev = await page.query_count(results) if results else 0
    iters = 0
    stopped = cap_label
    while iters < max_iters:
        if ctx.clock() >= deadline:
            stopped = "timeout"
            break
        if button is not None:
            if not await page.exists(button):
                stopped = "button_gone"
                break
            if not await page.is_enabled(button):
                stopped = "disabled"
                break
            try:
                await page.scroll_into_view(button)
                await page.click(button)
            except Exception:  # noqa: BLE001 - stale/detached button == exhausted
                stopped = "button_gone"
                break
        else:
            await page.scroll_to_bottom()
        iters += 1
        if results:
            grew = await _wait_for_growth(ctx, page, results, prev, settle_ms)
            new = await page.query_count(results)
            if not grew or new <= prev:
                stopped = "stable"
                break
            prev = new
    return {"iterations": iters, "final_count": prev, "stopped": stopped}


async def _act_load_more(ctx, page, *, button, results=None, max_clicks=200,
                         timeout_ms=60000, settle_ms=800, **_):
    return await _grow_loop(ctx, page, button=button, results=results,
                            max_iters=max_clicks, timeout_ms=timeout_ms,
                            settle_ms=settle_ms, cap_label="max_clicks")


async def _act_scroll_to_bottom(ctx, page, *, results=None, max_scrolls=50,
                                timeout_ms=30000, settle_ms=800, **_):
    return await _grow_loop(ctx, page, button=None, results=results,
                            max_iters=max_scrolls, timeout_ms=timeout_ms,
                            settle_ms=settle_ms, cap_label="max_scrolls")


async def _act_scroll(ctx, page, **_):
    await page.scroll_to_bottom()
    return {}


_PRIMITIVES: dict[str, Callable[..., Awaitable[dict]]] = {
    "goto": _act_goto,
    "wait_for_selector": _act_wait_for_selector,
    "wait_ms": _act_wait_ms,
    "click": _act_click,
    "fill": _act_fill,
    "press": _act_press,
    "eval_js": _act_eval_js,
    "screenshot": _act_screenshot,
    "load_more": _act_load_more,
    "scroll_to_bottom": _act_scroll_to_bottom,
    "scroll": _act_scroll,
}


# --------------------------------------------------------------------------- #
# The fetcher.
# --------------------------------------------------------------------------- #
class BrowserFetcher:
    """Run an interaction recipe in a browser and snapshot the result HTML."""

    def __init__(self, *, engine: str = "playwright", headless: bool = True,
                 timeout_secs: int = 30, proxy: Optional[str] = None,
                 user_agent: Optional[str] = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 clock: Callable[[], float] | None = None):
        self.engine = engine
        self.headless = headless
        self.timeout_secs = timeout_secs
        self.proxy = proxy
        self.user_agent = user_agent
        self._backend = None
        self._ctx = _ActionCtx(
            sleep=sleep or asyncio.sleep,
            clock=clock or time.monotonic,
            default_timeout_ms=timeout_secs * 1000,
        )

    async def _ensure_backend(self):
        if self._backend is None:
            if not browser_available(self.engine):
                raise BrowserError(
                    f"{self.engine} not installed: pip install 'ujin[browser]'"
                )
            if self.engine == "selenium":
                self._backend = _SeleniumBackend(self)
            else:
                self._backend = _PlaywrightBackend(self)
            await self._backend.start()
        return self._backend

    async def render(self, url: str, recipe: list[dict] | None = None, *,
                     results_selector: Optional[str] = None,
                     ctx: BuildContext | None = None) -> BrowserResult:
        backend = await self._ensure_backend()
        start = self._ctx.clock()
        page = await backend.new_page()
        actions_log: list[dict] = []
        screenshots: dict[str, bytes] = {}
        try:
            await page.goto(url)
            for step in (recipe or []):
                entry = await self._dispatch(page, step, ctx)
                data = entry.pop("_data", None)
                if data is not None:
                    screenshots[entry.get("name", f"shot{len(screenshots)}")] = data
                actions_log.append(entry)
            html = await page.content()
            final_url = await page.final_url()
            items = await page.harvest(results_selector) if results_selector else None
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
        return BrowserResult(
            url=url, html=html, items=items, actions_log=actions_log,
            elapsed_ms=int((self._ctx.clock() - start) * 1000),
            final_url=final_url, screenshots=screenshots or None,
        )

    async def _dispatch(self, page: _Page, step: dict,
                        ctx: BuildContext | None) -> dict:
        name = step.get("action")
        params = {k: v for k, v in step.items() if k != "action"}
        start = self._ctx.clock()
        try:
            if name in _PRIMITIVES:
                detail = await _PRIMITIVES[name](self._ctx, page, **params)
            elif register.has("action", name):
                actx = dataclasses.replace(ctx or BuildContext(),
                                           browser=self, page=page)
                handler = register.build_action(name, params, actx)
                detail = await handler(page, **params) or {}
            else:
                raise BrowserError(f"unknown action {name!r}")
            ok = True
        except Exception as exc:  # noqa: BLE001 - record + continue (get partial HTML)
            detail = {"error": f"{type(exc).__name__}: {exc}"}
            ok = False
        entry = {"action": name, "ok": ok,
                 "elapsed_ms": int((self._ctx.clock() - start) * 1000)}
        entry.update(detail or {})
        return entry

    async def close(self) -> None:
        if self._backend is not None:
            try:
                await self._backend.close()
            finally:
                self._backend = None


# --------------------------------------------------------------------------- #
# Playwright backend (async).
#
# Everything below requires a real installed browser (playwright chromium or
# chromedriver); it is exercised by the `browser`-marked integration tests
# (tests/integration/), never by the offline unit suite — hence no cover.
# --------------------------------------------------------------------------- #
class _PlaywrightBackend:  # pragma: no cover
    def __init__(self, fetcher: BrowserFetcher):
        self._f = fetcher
        self._pw = None
        self._browser = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._f.headless)

    async def new_page(self) -> "_PlaywrightPage":
        kwargs: dict[str, Any] = {}
        if self._f.user_agent:
            kwargs["user_agent"] = self._f.user_agent
        if self._f.proxy:
            kwargs["proxy"] = {"server": self._f.proxy}
        context = await self._browser.new_context(**kwargs)
        page = await context.new_page()
        return _PlaywrightPage(context, page, self._f.timeout_secs * 1000)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()


class _PlaywrightPage:  # pragma: no cover
    def __init__(self, context, page, default_timeout_ms: int):
        self._context = context
        self._p = page
        self._to = default_timeout_ms

    async def goto(self, url: str) -> None:
        await self._p.goto(url, wait_until="domcontentloaded", timeout=self._to)

    async def content(self) -> str:
        return await self._p.content()

    async def final_url(self) -> str:
        return self._p.url

    async def click(self, selector: str) -> None:
        await self._p.click(selector, timeout=self._to)

    async def fill(self, selector: str, value: str) -> None:
        await self._p.fill(selector, value, timeout=self._to)

    async def press(self, selector: str, key: str) -> None:
        await self._p.press(selector, key, timeout=self._to)

    async def query_count(self, selector: str) -> int:
        return await self._p.locator(selector).count()

    async def exists(self, selector: str) -> bool:
        return await self._p.locator(selector).count() > 0

    async def is_enabled(self, selector: str) -> bool:
        try:
            return await self._p.locator(selector).first.is_enabled(timeout=1000)
        except Exception:  # noqa: BLE001
            return False

    async def wait_for_selector(self, selector: str, timeout_ms: int) -> None:
        await self._p.wait_for_selector(selector, timeout=timeout_ms)

    async def scroll_into_view(self, selector: str) -> None:
        await self._p.locator(selector).first.scroll_into_view_if_needed(timeout=self._to)

    async def scroll_to_bottom(self) -> None:
        await self._p.evaluate("window.scrollTo(0, document.body.scrollHeight)")

    async def eval_js(self, script: str) -> Any:
        return await self._p.evaluate(script)

    async def screenshot(self) -> bytes:
        return await self._p.screenshot()

    async def harvest(self, selector: str) -> list[dict]:
        loc = self._p.locator(selector)
        n = await loc.count()
        out = []
        for i in range(n):
            el = loc.nth(i)
            try:
                text = (await el.inner_text())[:500].strip()
            except Exception:  # noqa: BLE001
                text = ""
            href = await el.get_attribute("href")
            out.append({"text": text, "href": href})
        return out

    async def close(self) -> None:
        await self._context.close()


# --------------------------------------------------------------------------- #
# Selenium backend (sync WebDriver on a dedicated single thread).
# --------------------------------------------------------------------------- #
class _SeleniumBackend:  # pragma: no cover
    def __init__(self, fetcher: BrowserFetcher):
        self._f = fetcher
        self._driver = None
        self._executor = None

    async def start(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        # WebDriver is not thread-safe: pin every call to one worker thread.
        self._executor = ThreadPoolExecutor(max_workers=1)
        await self._run(self._build_driver)

    def _build_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        opts = Options()
        if self._f.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        if self._f.user_agent:
            opts.add_argument(f"--user-agent={self._f.user_agent}")
        if self._f.proxy:
            opts.add_argument(f"--proxy-server={self._f.proxy}")
        self._driver = webdriver.Chrome(options=opts)
        self._driver.set_page_load_timeout(self._f.timeout_secs)

    async def _run(self, fn, *a):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*a))

    async def new_page(self) -> "_SeleniumPage":
        return _SeleniumPage(self)

    async def close(self) -> None:
        if self._driver is not None:
            await self._run(self._driver.quit)
        if self._executor is not None:
            self._executor.shutdown(wait=True)


class _SeleniumPage:  # pragma: no cover
    """Async adapter over the single shared sync WebDriver."""

    def __init__(self, backend: _SeleniumBackend):
        self._b = backend
        self._d = backend._driver

    async def _run(self, fn, *a):
        return await self._b._run(fn, *a)

    def _by(self, selector: str):
        from selenium.webdriver.common.by import By

        return (By.CSS_SELECTOR, selector)

    async def goto(self, url: str) -> None:
        await self._run(self._d.get, url)

    async def content(self) -> str:
        return await self._run(lambda: self._d.page_source)

    async def final_url(self) -> str:
        return await self._run(lambda: self._d.current_url)

    async def _find(self, selector: str):
        return self._d.find_element(*self._by(selector))

    async def click(self, selector: str) -> None:
        await self._run(lambda: self._d.find_element(*self._by(selector)).click())

    async def fill(self, selector: str, value: str) -> None:
        await self._run(lambda: self._d.find_element(*self._by(selector)).send_keys(value))

    async def press(self, selector: str, key: str) -> None:
        from selenium.webdriver.common.keys import Keys

        keyval = getattr(Keys, key.upper(), key)
        await self._run(lambda: self._d.find_element(*self._by(selector)).send_keys(keyval))

    async def query_count(self, selector: str) -> int:
        return await self._run(lambda: len(self._d.find_elements(*self._by(selector))))

    async def exists(self, selector: str) -> bool:
        return await self.query_count(selector) > 0

    async def is_enabled(self, selector: str) -> bool:
        def _check():
            els = self._d.find_elements(*self._by(selector))
            return bool(els) and els[0].is_enabled() and els[0].is_displayed()
        try:
            return await self._run(_check)
        except Exception:  # noqa: BLE001
            return False

    async def wait_for_selector(self, selector: str, timeout_ms: int) -> None:
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        def _wait():
            WebDriverWait(self._d, timeout_ms / 1000).until(
                EC.presence_of_element_located(self._by(selector)))
        await self._run(_wait)

    async def scroll_into_view(self, selector: str) -> None:
        def _scroll():
            el = self._d.find_element(*self._by(selector))
            self._d.execute_script("arguments[0].scrollIntoView();", el)
        await self._run(_scroll)

    async def scroll_to_bottom(self) -> None:
        await self._run(lambda: self._d.execute_script(
            "window.scrollTo(0, document.body.scrollHeight)"))

    async def eval_js(self, script: str) -> Any:
        return await self._run(lambda: self._d.execute_script(script))

    async def screenshot(self) -> bytes:
        return await self._run(self._d.get_screenshot_as_png)

    async def harvest(self, selector: str) -> list[dict]:
        def _harvest():
            out = []
            for el in self._d.find_elements(*self._by(selector)):
                out.append({"text": (el.text or "")[:500].strip(),
                            "href": el.get_attribute("href")})
            return out
        return await self._run(_harvest)

    async def close(self) -> None:
        # one driver/one page per backend lifetime; closing happens in backend.close
        return None
