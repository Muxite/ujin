"""Site-profile registry — add a marketplace by adding a profile, not code.

A profile says how to search a site (URL template), how to read its product cards (CSS
selector overrides; JSON-LD/OpenGraph need none), the render engine, and a default per-category
keyterm bank. ``MarketplaceSearchPollable`` samples a few (category, term) pairs per run and
scrapes each via the generic, site-agnostic :class:`~ujin.poll.amazon.AmazonSearchPollable`.

To add a site: add an entry to ``SITE_PROFILES`` and point a workflow at ``marketplace_search``.
"""
from __future__ import annotations

import asyncio
import logging
import random as _random_mod

from ujin.poll.amazon import AmazonSearchPollable
from ujin.poll.base import PollResult, decide_changed, fingerprint

log = logging.getLogger("ujin.poll.marketplace")


SITE_PROFILES: dict[str, dict] = {
    "amazon": {
        "domain": "amazon.com",
        "search_url": "https://{domain}/s?k={query}",
        "selectors": None,                       # Amazon defaults live in extract/product.py
        "engine": "auto",
        "wait_selector": "div[data-component-type='s-search-result']",
        "keyterms": {},                          # use amazon_category for Amazon sweeps
    },
    # PC components. Newegg is JS-heavy, so default to the browser engine.
    "newegg": {
        "domain": "newegg.com",
        "search_url": "https://www.{domain}/p/pl?d={query}",
        "selectors": {
            "card": ".item-cell",
            "id_attr": "data-id",
            "title": (".item-title",),
            "image": (".item-img img", "img"),
            "price": (".price-current", ".price-current strong"),
            "link": "a.item-title",
        },
        "engine": "browser",
        "wait_selector": ".item-cell",
        "keyterms": {
            "RAM": ["ddr4 ram", "ddr5 ram", "16gb ram", "32gb ram", "ddr5 6000"],
            "SSD": ["nvme ssd", "sata ssd", "1tb ssd", "2tb nvme ssd", "m.2 ssd"],
            "HDD": ["internal hard drive", "2tb hard drive", "4tb hard drive", "external hdd"],
        },
    },
    # eBay. The browser engine (with the stealth context) is required: the bare-headless
    # fingerprint is bounced to an error page, AND the classic `/sch/i.html` search route is
    # blocked even with stealth — but `/sch/?_nkw=` serves the full result grid (verified
    # live: 70+ cards). Current SRP uses `.s-card`; legacy `.s-item` selectors are kept as
    # fallbacks. The id is recovered from each card's `/itm/<digits>` link
    # (see _HREF_ID_PATTERNS["ebay"]); eBay's first "Shop on eBay" promo card is skipped.
    "ebay": {
        "domain": "ebay.com",
        "search_url": "https://www.{domain}/sch/?_nkw={query}",
        "selectors": {
            "card": ".s-card, .s-item",
            "id_attr": "data-id",            # absent on eBay cards -> id from /itm/ link
            "title": (".s-card__title", ".s-item__title span", ".s-item__title", "h3"),
            "image": (".s-card__image img", ".s-item__image-wrapper img", "img"),
            "price": (".s-card__price", ".s-item__price"),
            "link": ("a.su-link", "a.s-item__link", "a[href*='/itm/']"),
        },
        "engine": "browser",
        "wait_selector": ".s-card, .s-item",
        "keyterms": {
            "Electronics": ["wireless earbuds", "bluetooth speaker", "smart watch",
                            "gaming mouse", "graphics card", "drone"],
            "Collectibles": ["pokemon cards", "vintage camera", "lego set",
                             "action figure", "comic book"],
            "Apparel": ["leather jacket", "running shoes", "designer handbag",
                        "mechanical watch", "sunglasses"],
            "Home": ["espresso machine", "cast iron skillet", "power tool", "vacuum cleaner"],
        },
    },
    # Walmart. Protected by PerimeterX ("Robot or human?") — best effort. Cards carry a
    # numeric `data-item-id`; the JSON-LD detail path enriches each item page when reachable.
    "walmart": {
        "domain": "walmart.com",
        "search_url": "https://www.{domain}/search?q={query}",
        "selectors": {
            "card": "[data-item-id]",
            "id_attr": "data-item-id",
            "title": ("[data-automation-id='product-title']", "span.w_iUH7", "a span"),
            "image": ("img[data-testid='productTileImage']", "img[loading]", "img"),
            "price": ("[data-automation-id='product-price'] .w_iUH7",
                      "[data-automation-id='product-price']",
                      "div[data-automation-id='product-price']"),
            "link": ("a[link-identifier]", "a[href*='/ip/']"),
        },
        "engine": "browser",
        "wait_selector": "[data-item-id]",
        "keyterms": {
            "Grocery": ["coffee", "olive oil", "protein powder", "cereal"],
            "Home": ["bed sheets", "throw pillow", "storage bin", "area rug"],
            "Electronics": ["bluetooth speaker", "tablet", "headphones", "smart bulb"],
            "Toys": ["board game", "building blocks", "remote control car"],
        },
    },
}


class MarketplaceSearchPollable:
    """Scrape a sample of a site profile's keyterms per poll -> combined product list."""

    def __init__(
        self,
        *,
        profile: str = "amazon",
        categories: dict[str, list[str]] | None = None,
        terms_per_poll: int = 3,
        max_results: int = 8,
        engine: str | None = None,
        proxy: str | None = None,
        timeout_secs: int = 40,
        headless: bool = True,
        seed: int | None = None,
        with_description: bool = False,
        key: str | None = None,
    ) -> None:
        self.profile_name = profile if profile in SITE_PROFILES else "amazon"
        self.profile = SITE_PROFILES[self.profile_name]
        self.categories = categories or self.profile.get("keyterms") or {}
        self.terms_per_poll = max(1, int(terms_per_poll))
        self.max_results = max(1, int(max_results))
        self.engine = engine or self.profile.get("engine", "auto")
        self.proxy = proxy
        self.timeout_secs = timeout_secs
        self.headless = headless
        self.with_description = with_description
        self.key = key or f"marketplace:{self.profile_name}"
        self._rng = _random_mod.Random(seed)

    def _child(self, term: str, category: str | None) -> AmazonSearchPollable:
        return AmazonSearchPollable(
            term,
            domain=self.profile["domain"],
            max_results=self.max_results,
            category=category,
            engine=self.engine,
            headless=self.headless,
            proxy=self.proxy,
            timeout_secs=self.timeout_secs,
            source=self.profile_name,
            selectors=self.profile.get("selectors"),
            search_url_template=self.profile["search_url"],
            wait_selector=self.profile.get("wait_selector"),
            with_description=self.with_description,
            desc_selectors=self.profile.get("desc_selectors"),
        )

    def _sample(self) -> list[tuple[str, str]]:
        pairs = [(t, cat) for cat, terms in self.categories.items() for t in terms]
        if not pairs:
            return []
        k = min(self.terms_per_poll, len(pairs))
        return self._rng.sample(pairs, k)

    async def poll(self, prev: PollResult | None) -> PollResult:
        pairs = self._sample()
        log.info("marketplace[%s] sweep: %s", self.profile_name, [t for t, _ in pairs])
        children = [self._child(term, cat) for term, cat in pairs]
        results = await asyncio.gather(*(c.poll(None) for c in children), return_exceptions=True)
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
        return PollResult(
            ok=True,
            changed=decide_changed(fingerprint(combined), prev),
            fingerprint=fingerprint(combined),
            payload=combined,
        )
