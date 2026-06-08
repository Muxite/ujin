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
        clean_titles: bool = True,
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
        self.clean_titles = clean_titles
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

        from ujin.extract.product import clean_product_name, extract_products

        products = extract_products(html, url, source="amazon")[: self.max_results]
        for p in products:
            if self.category:
                p.category = self.category
            if self.clean_titles:
                p.title = clean_product_name(p.title)
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


# Generic, brand-agnostic product queries per category. We don't care which
# specific item comes back — any item with a name/price/picture is usable — so
# these are common product nouns, not models. Extend freely.
_KEYTERM_BANK: dict[str, list[str]] = {
    "Electronics": ["wireless earbuds", "bluetooth speaker", "mechanical keyboard",
                    "usb c charger", "gaming mouse", "webcam", "portable ssd",
                    "smart watch", "power bank", "wifi router", "noise cancelling headphones",
                    "tablet", "hdmi cable", "phone case"],
    "Kitchen": ["air fryer", "coffee maker", "blender", "chef knife", "cast iron skillet",
                "food storage containers", "electric kettle", "water bottle",
                "cutting board", "toaster", "mixing bowls", "travel mug"],
    "Home": ["led strip lights", "air purifier", "robot vacuum", "throw blanket",
             "desk lamp", "picture frame", "storage bins", "wall clock",
             "mattress topper", "shower curtain", "scented candle"],
    "Tools": ["cordless drill", "tape measure", "screwdriver set", "tool box",
              "work gloves", "flashlight", "level tool", "stud finder", "utility knife"],
    "Toys": ["building blocks", "board game", "action figure", "jigsaw puzzle",
             "remote control car", "plush toy", "card game", "play kitchen"],
    "Beauty": ["face moisturizer", "hair dryer", "electric toothbrush", "makeup brushes",
               "sunscreen", "beard trimmer", "nail kit", "facial cleanser"],
    "Sports": ["yoga mat", "dumbbells", "resistance bands", "jump rope", "foam roller",
               "running belt", "basketball", "camping chair", "hiking backpack"],
    "Office": ["desk organizer", "mechanical pencil", "notebook", "monitor stand",
               "label maker", "sticky notes", "desk mat", "file folders"],
    "Apparel": ["running shoes", "backpack", "baseball cap", "wool socks",
                "sunglasses", "winter gloves", "rain jacket", "leather belt"],
}

# Occasionally appended/prepended to drift the result set toward fresh items.
# Mostly empty so a "change" only happens once in a while.
_MODIFIERS = ["", "", "", "", "best", "premium", "portable", "set", "for home", "2024"]


class AmazonCategoryPollable:
    """Randomized, category-driven Amazon sweep.

    Each poll samples a handful of generic product queries from the keyterm bank
    (optionally restricted to ``categories``), occasionally mutating a term with a
    random modifier so the result set drifts over time and surfaces new items.
    Every sampled term is scraped via :class:`AmazonSearchPollable` (so engine
    escalation, sponsored-skipping and title cleaning all apply) and the products
    are combined + deduped into one batch tagged with their category.
    """

    def __init__(
        self,
        *,
        categories: list[str] | None = None,
        terms_per_poll: int = 3,
        max_results: int = 4,
        mutate_prob: float = 0.25,
        domain: str = "amazon.com",
        engine: str = "auto",
        headless: bool = True,
        proxy: str | None = None,
        timeout_secs: int = 30,
        seed: int | None = None,
        key: str = "amazon_category",
    ) -> None:
        self.categories = [c for c in (categories or list(_KEYTERM_BANK)) if c in _KEYTERM_BANK]
        if not self.categories:
            self.categories = list(_KEYTERM_BANK)
        self.terms_per_poll = max(1, int(terms_per_poll))
        self.max_results = max(1, int(max_results))
        self.mutate_prob = mutate_prob
        self.domain = domain
        self.engine = engine
        self.headless = headless
        self.proxy = proxy
        self.timeout_secs = timeout_secs
        self.key = key
        import random as _random
        self._rng = _random.Random(seed)

    def _sample_terms(self) -> list[tuple[str, str]]:
        """Pick (term, category) pairs for this poll, with occasional mutation."""
        pairs = [(t, cat) for cat in self.categories for t in _KEYTERM_BANK[cat]]
        k = min(self.terms_per_poll, len(pairs))
        chosen = self._rng.sample(pairs, k)
        out: list[tuple[str, str]] = []
        for term, cat in chosen:
            if self._rng.random() < self.mutate_prob:
                mod = self._rng.choice(_MODIFIERS)
                if mod:
                    term = f"{mod} {term}" if self._rng.random() < 0.5 else f"{term} {mod}"
            out.append((term, cat))
        return out

    async def poll(self, prev: PollResult | None) -> PollResult:
        import asyncio

        pairs = self._sample_terms()
        log.info("amazon_category sweep: %s", [t for t, _ in pairs])
        children = [
            AmazonSearchPollable(
                term, domain=self.domain, max_results=self.max_results, category=cat,
                engine=self.engine, headless=self.headless, proxy=self.proxy,
                timeout_secs=self.timeout_secs,
            )
            for term, cat in pairs
        ]
        results = await asyncio.gather(*(c.poll(None) for c in children),
                                       return_exceptions=True)
        combined: list[dict] = []
        seen: set[str] = set()
        for res in results:
            if isinstance(res, Exception) or not getattr(res, "ok", False):
                continue
            for item in (res.payload or []):
                sid = item.get("source_id")
                if sid and sid in seen:
                    continue
                if sid:
                    seen.add(sid)
                combined.append(item)
        # Always push what we found this cycle (upsert is idempotent), so
        # last_seen_at stays fresh and new items land.
        return PollResult(ok=True, changed=bool(combined),
                          fingerprint=fingerprint(combined), payload=combined)
