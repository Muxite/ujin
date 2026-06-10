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
import json
import logging
import os
import random as _random_mod
import re
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
    """Poll one marketplace search term -> list of product dicts.

    Defaults target Amazon, but ``source``/``selectors``/``search_url_template``/
    ``wait_selector`` make it site-agnostic — drive any marketplace from a site profile
    (see ujin/ujin/poll/marketplace.py) without new code.
    """

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
        source: str = "amazon",
        selectors: dict | None = None,
        search_url_template: str | None = None,
        wait_selector: str | None = None,
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
        self.source = source
        self.selectors = selectors or None
        self.search_url_template = search_url_template or "https://{domain}/s?k={query}"
        self.wait_selector = wait_selector or "div[data-component-type='s-search-result']"
        self.key = key or f"{source}:{term}"

    @property
    def search_url(self) -> str:
        return self.search_url_template.format(domain=self.domain, query=quote_plus(self.term))

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
                       "selector": self.wait_selector,
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

                if extract_products(html, url, source=self.source, selectors=self.selectors):
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

        products = extract_products(html, url, source=self.source, selectors=self.selectors)[: self.max_results]
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


# ── Keyterm harvesting ──────────────────────────────────────────────────────
# Self-expanding search variety with no LLM: every scraped product title is a bag
# of real product words. We pull the uncommon ones out, remember the ones we've
# never searched, and feed them back as future queries. A search for "razor" surfaces
# titles like "stainless steel safety razor blades" -> we learn "stainless", "safety",
# "blades" and search those next. The `used` set guarantees a term is never searched
# twice, so coverage keeps widening instead of looping.

_WORD_RE = re.compile(r"[a-z][a-z]+")

# Words too generic to be useful queries (articles, filler, colors, units, sizes,
# materials/adjectives that match everything) plus the drift modifiers above. Harvested
# words are also length-gated, so this list only needs the common *long* offenders.
_HARVEST_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "from", "your", "you", "our", "this", "that", "these",
    "those", "all", "any", "each", "per", "set", "pack", "piece", "pieces", "count",
    "pcs", "size", "large", "small", "medium", "mini", "max", "plus", "pro", "premium",
    "best", "new", "high", "quality", "professional", "portable", "wireless", "rechargeable",
    "adjustable", "universal", "multi", "multiple", "double", "single", "heavy", "duty",
    "duty", "super", "ultra", "extra", "long", "short", "wide", "thick", "thin", "soft",
    "hard", "light", "lightweight", "durable", "strong", "easy", "fast", "quick", "smart",
    "black", "white", "blue", "red", "green", "gray", "grey", "silver", "gold", "pink",
    "purple", "yellow", "brown", "orange", "color", "colour", "colors", "colours",
    "inch", "inches", "feet", "foot", "pack", "packs", "piece", "pound", "pounds", "ounce",
    "ounces", "gram", "grams", "liter", "litre", "men", "women", "kids", "boys", "girls",
    "unisex", "home", "office", "indoor", "outdoor", "travel", "gift", "gifts", "use",
    "used", "include", "includes", "including", "free", "case", "cover", "kit", "accessory",
    "accessories", "compatible", "replacement", "original", "genuine", "official", "style",
    "design", "fashion", "classic", "modern", "deluxe", "standard", "edition", "version",
    "type", "model", "series", "brand", "value", "great", "perfect", "ideal", "non",
    "anti", "inch", "cm", "mm", "kg", "ml", "oz", "led", "usb",
})


class _HarvestStore:
    """Persistent term pool + already-searched set, stored as one JSON file.

    ``pool`` maps a not-yet-searched term -> the category of the listing it came from
    (so when we search it, the results get tagged with a sensible category). ``used`` is
    every term we've already issued a search for — terms never re-enter the pool.
    """

    def __init__(self, path: str, max_pool: int = 5000, rng=None) -> None:
        self.path = path
        self.max_pool = max_pool
        self._rng = rng or _random_mod.Random()
        self.used: set[str] = set()
        self.pool: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.used = set(data.get("used", []))
            self.pool = {str(k): str(v) for k, v in dict(data.get("pool", {})).items()}
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            pass  # first run / unreadable -> start empty

    def save(self) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"used": sorted(self.used), "pool": self.pool}, f)
            os.replace(tmp, self.path)  # atomic
        except OSError as exc:  # noqa: BLE001
            log.warning("harvest store save failed (%s): %s", self.path, exc)

    def add_from_titles(self, items: list[dict], min_len: int) -> int:
        """Harvest new words from scraped titles. Returns how many were added."""
        added = 0
        for item in items:
            title = str(item.get("title") or "")
            category = str(item.get("category") or "") or "Electronics"
            for word in _WORD_RE.findall(title.lower()):
                if len(word) < min_len or word in _HARVEST_STOPWORDS:
                    continue
                if word in self.used or word in self.pool:
                    continue
                if len(self.pool) >= self.max_pool:
                    return added
                self.pool[word] = category
                added += 1
        return added

    def draw(self, n: int) -> list[tuple[str, str]]:
        """Take up to ``n`` terms from the pool, marking them used (never reissued)."""
        if n <= 0 or not self.pool:
            return []
        terms = self._rng.sample(list(self.pool), min(n, len(self.pool)))
        out: list[tuple[str, str]] = []
        for term in terms:
            out.append((term, self.pool.pop(term)))
            self.used.add(term)
        return out


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
        harvest: bool = False,
        harvest_path: str = "/data/amazon_harvest.json",
        harvest_ratio: float = 0.5,
        harvest_min_len: int = 4,
        max_pool: int = 5000,
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
        self._rng = _random_mod.Random(seed)
        # Harvest config: fraction of each poll's terms drawn from words learned from
        # previous results (the rest come from the static bank so we never starve).
        self.harvest = bool(harvest)
        self.harvest_ratio = min(1.0, max(0.0, float(harvest_ratio)))
        self.harvest_min_len = max(3, int(harvest_min_len))
        self._store = (
            _HarvestStore(harvest_path, max_pool=max_pool, rng=self._rng)
            if self.harvest else None
        )

    def _sample_terms(self) -> list[tuple[str, str]]:
        """Pick (term, category) pairs for this poll, with occasional mutation.

        When harvesting is on, up to ``harvest_ratio`` of the terms are drawn from the
        learned pool (each used at most once, ever); the remainder come from the static
        bank, so a run always has something to search even when the pool is empty.
        """
        out: list[tuple[str, str]] = []

        # 1. Harvested terms (already-deduped against the used set). No mutation — these
        #    are real product words and we want to search them verbatim.
        if self._store is not None and self.harvest_ratio > 0:
            want = round(self.terms_per_poll * self.harvest_ratio)
            out.extend(self._store.draw(want))

        # 2. Fill the rest from the static bank, with occasional drift.
        remaining = self.terms_per_poll - len(out)
        if remaining > 0:
            pairs = [(t, cat) for cat in self.categories for t in _KEYTERM_BANK[cat]]
            chosen = self._rng.sample(pairs, min(remaining, len(pairs)))
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

        # Learn new query terms from this cycle's titles for future polls, then persist.
        if self._store is not None:
            added = self._store.add_from_titles(combined, self.harvest_min_len)
            self._store.save()
            log.info("amazon_category harvest: +%d terms (pool=%d, used=%d)",
                     added, len(self._store.pool), len(self._store.used))

        # Always push what we found this cycle (upsert is idempotent), so
        # last_seen_at stays fresh and new items land.
        return PollResult(ok=True, changed=bool(combined),
                          fingerprint=fingerprint(combined), payload=combined)
