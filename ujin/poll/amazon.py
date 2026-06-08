"""AmazonSearchPollable — poll an Amazon search and return normalized products.

Builds a search URL from a query term, fetches the results page, and feeds the
HTML to :func:`ujin.extract.product.extract_products`. The ``payload`` is a
**list of product dicts** (``source, source_id, title, image_url, price_cents,
currency, category, url``) — exactly what the ``select``/``dedupe`` transforms
and the sinks consume.

Render engine is pluggable and degrades gracefully (``engine="auto"``):

  http      -> HttpFetcher (fast; often bot-blocked by Amazon)
  obscura   -> ObscuraFetcher (ujin's headless snapshot renderer; OBSCURA_BIN/URL)
  browser   -> BrowserFetcher (Playwright/Selenium; needs ujin[browser])
  auto      -> try http, escalate to obscura then browser until products appear

Amazon aggressively blocks datacenter IPs; a ``proxy`` (or the ``PROXY_URL``
env) routes fetches through an upstream proxy. A run that finds nothing returns
an empty ``changed=False`` payload rather than raising, so a workflow stays
healthy across transient blocks.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from urllib.parse import quote_plus

from ujin.poll.base import PollResult, decide_changed, fingerprint

log = logging.getLogger("ujin.poll.amazon")

# Ordered escalation for engine="auto".
_AUTO_CHAIN = ("http", "obscura", "browser")

# A realistic desktop UA improves the odds against light bot heuristics.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class AmazonSearchPollable:
    """Poll one Amazon search term -> list of product dicts."""

    def __init__(
        self,
        term: str,
        *,
        domain: str = "amazon.com",
        max_results: int = 1,
        category: str | None = None,
        engine: str = "auto",
        headless: bool = True,
        proxy: str | None = None,
        timeout_secs: int = 30,
        key: str | None = None,
    ) -> None:
        self.term = term
        self.domain = domain
        self.max_results = max(1, int(max_results))
        self.category = category
        self.engine = engine
        self.headless = headless
        self.proxy = proxy or os.environ.get("PROXY_URL") or None
        self.timeout_secs = timeout_secs
        self.key = key or f"amazon:{term}"

    @property
    def search_url(self) -> str:
        return f"https://{self.domain}/s?k={quote_plus(self.term)}"

    async def _fetch_http(self, url: str) -> str:
        from ujin.fetch.http import HttpFetcher

        async with HttpFetcher(user_agent=_UA, timeout_secs=self.timeout_secs) as f:
            resp = await f.get(url, proxy=self.proxy)
            return resp.body or ""

    async def _fetch_obscura(self, url: str) -> str:
        from ujin.fetch.obscura import ObscuraFetcher, obscura_available

        if not obscura_available():
            return ""
        return (await ObscuraFetcher(timeout_secs=self.timeout_secs).render_html(url)).html or ""

    async def _fetch_browser(self, url: str) -> str:
        from ujin.fetch.browser import BrowserFetcher, browser_available

        engine = "selenium" if self.engine == "selenium" else "playwright"
        if not browser_available(engine):
            return ""
        fetcher = BrowserFetcher(
            engine=engine, headless=self.headless,
            timeout_secs=self.timeout_secs, proxy=self.proxy, user_agent=_UA,
        )
        try:
            recipe = [{"action": "wait_for_selector",
                       "selector": "div[data-component-type='s-search-result']",
                       "timeout_ms": self.timeout_secs * 1000}]
            return (await fetcher.render(url, recipe)).html or ""
        finally:
            try:
                await fetcher.close()
            except Exception:  # noqa: BLE001
                pass

    async def _render(self, url: str) -> tuple[str, str]:
        """Return (html, engine_used). Escalates through the auto chain."""
        engines = _AUTO_CHAIN if self.engine == "auto" else (self.engine,)
        last = ""
        for eng in engines:
            try:
                if eng == "http":
                    html = await self._fetch_http(url)
                elif eng == "obscura":
                    html = await self._fetch_obscura(url)
                else:  # browser / playwright / selenium
                    html = await self._fetch_browser(url)
            except Exception as exc:  # noqa: BLE001
                log.warning("amazon %s fetch via %s failed: %s", self.term, eng, exc)
                html = ""
            if html:
                # Accept this engine if products parse; otherwise keep the HTML
                # as a fallback and escalate to the next engine.
                from ujin.extract.product import extract_products

                if extract_products(html, url, source="amazon"):
                    return html, eng
                last = html
        return last, engines[-1]

    async def poll(self, prev: PollResult | None) -> PollResult:
        url = self.search_url
        html, engine_used = await self._render(url)
        if not html:
            log.warning("amazon %s: no HTML from any engine", self.term)
            return PollResult(ok=True, changed=False, fingerprint=None, payload=[])

        from ujin.extract.product import extract_products

        products = extract_products(html, url, source="amazon")[: self.max_results]
        for p in products:
            if self.category:
                p.category = self.category
        items = [dataclasses.asdict(p) for p in products]
        if not items:
            log.warning("amazon %s: page fetched (%s) but no products parsed",
                        self.term, engine_used)
        fp = fingerprint(items)
        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload=items,
        )
