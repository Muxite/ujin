"""BrowserPollable — poll a page through a browser interaction recipe.

Drives :class:`ujin.fetch.browser.BrowserFetcher` (Playwright/Selenium) to run a
declarative recipe (e.g. ``load_more`` until exhausted), then feeds the final,
fully-loaded HTML to the existing extractors. The result ``payload`` is a **list
of dicts** (for ``extract=links``) — exactly what the ``chunk``/``dedupe``
transforms and the sinks consume — so a "load every publication" recipe lands
LLM-ready.

``extract`` selects what comes back:
  links      -> extract_headline_links(...)  (list[dict])     [default]
  article    -> extract_article(...)          (dict | None)
  structured -> extract_structured(...)       (dict)
  raw        -> harvested items, or the raw HTML when no results_selector
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any

from ujin.poll.base import PollResult, decide_changed, fingerprint


class BrowserPollable:
    def __init__(
        self,
        url: str,
        *,
        engine: str = "playwright",
        actions: list[dict] | None = None,
        extract: str = "links",
        results_selector: str | None = None,
        headless: bool = True,
        key: str | None = None,
        fetcher: Any = None,
        ctx: Any = None,
    ) -> None:
        self.url = url
        self.engine = engine
        self.actions = list(actions or [])
        self.extract = extract
        self.results_selector = results_selector
        self.headless = headless
        self.key = key or f"browser:{extract}:{url}"
        self._fetcher = fetcher
        self._ctx = ctx

    async def _get_fetcher(self):
        if self._fetcher is None:
            from ujin.fetch.browser import BrowserFetcher

            self._fetcher = BrowserFetcher(engine=self.engine, headless=self.headless)
        return self._fetcher

    async def poll(self, prev: PollResult | None) -> PollResult:
        start = time.monotonic()
        try:
            fetcher = await self._get_fetcher()
            r = await fetcher.render(self.url, self.actions,
                                     results_selector=self.results_selector,
                                     ctx=self._ctx)
        except Exception as exc:  # noqa: BLE001
            return PollResult.failure(f"{type(exc).__name__}: {exc}")

        base = r.final_url or self.url
        if self.extract == "links":
            from ujin.extract.links import extract_headline_links

            payload: Any = [dataclasses.asdict(l)
                            for l in extract_headline_links(r.html, base_url=base)]
        elif self.extract == "article":
            from ujin.extract.article import extract_article

            art = extract_article(r.html, url=base)
            payload = dataclasses.asdict(art) if art is not None else None
        elif self.extract == "structured":
            from ujin.extract.structured import extract_structured

            payload = extract_structured(r.html)
        else:  # "raw"
            payload = r.items if r.items is not None else r.html

        fp = fingerprint(payload)
        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload=payload,
            latency_ms=r.elapsed_ms or int((time.monotonic() - start) * 1000),
            status=200,
        )
