"""Site-profile registry — add a marketplace by adding a profile, not code.

A profile says how to search a site (URL template), how to read its product cards (CSS
selector overrides; JSON-LD/OpenGraph need none), the render engine, and a default per-category
keyterm bank. ``MarketplaceSearchPollable`` samples a few (category, term) pairs per run and
scrapes each via the generic, site-agnostic :class:`~ujin.poll.amazon.AmazonSearchPollable`.

To add a site: add an entry to ``SITE_PROFILES`` and point a workflow at ``marketplace_search``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random as _random_mod
import time

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
    # BEST-EFFORT + PROXY: needs a residential PROXY_URL for reliable datacenter-IP runs.
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
    # ── JSON-LD-first profiles (selectors: None ⇒ extract_products auto-reads schema.org
    #    Product/OpenGraph; detail pages enrich via the generic _detail_from_jsonld path).
    #    These are the most reliable: no per-site CSS to drift, and a clean Product block
    #    gives brand/rating/reviews/specs for free. Search-results pages on most of these
    #    sites are JS-rendered, so the engine defaults to "browser" + a wait_selector. ──
    #
    # AliExpress. Heavy baxia/anti-bot wall — BEST-EFFORT + residential PROXY required for
    # reliable runs (a datacenter IP gets an anti-bot shell with no product modules). Search
    # results are JS-rendered; the dedicated `_aliexpress_detail` extractor parses each item
    # page's `window.runParams` (richer than JSON-LD). Card extraction falls back to JSON-LD
    # where present. source_id is the numeric /item/<id>.html.
    "aliexpress": {
        "domain": "aliexpress.com",
        "search_url": "https://www.{domain}/w/wholesale-{query}.html",
        "selectors": None,                       # JSON-LD/OpenGraph cards; detail via runParams
        "engine": "browser",
        "wait_selector": "a[href*='/item/']",
        "keyterms": {
            "Electronics": ["wireless earbuds", "phone case", "smart watch", "led strip",
                            "usb c cable", "bluetooth speaker"],
            "Home": ["kitchen gadget", "storage organizer", "wall sticker", "throw pillow cover"],
            "Hobby": ["fishing rod", "model kit", "watercolor set", "knitting needles"],
            "Apparel": ["graphic t shirt", "baseball cap", "backpack", "sunglasses"],
        },
    },
    # Target. RedSky-backed catalogue ships schema.org Product JSON-LD on item pages; SRP is
    # JS-rendered. RELIABLE (light wall) but a proxy helps under heavy sweeps. source_id is the
    # numeric /p/<slug>/-/A-<id> — recovered from the link (see _HREF_ID_PATTERNS["target"]).
    "target": {
        "domain": "target.com",
        "search_url": "https://www.{domain}/s?searchTerm={query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "[data-test='product-title'], a[href*='/p/']",
        "keyterms": {
            "Home": ["throw blanket", "storage bin", "desk lamp", "bath towel set", "area rug"],
            "Kitchen": ["coffee maker", "air fryer", "dinnerware set", "water bottle"],
            "Toys": ["building blocks", "board game", "stuffed animal", "play kitchen"],
            "Beauty": ["face moisturizer", "shampoo", "makeup brushes", "sunscreen"],
        },
    },
    # Best Buy. Electronics retailer; product pages carry schema.org Product JSON-LD. RELIABLE
    # for detail enrichment; SRP is JS-rendered. source_id is the numeric skuId in /site/.../<id>.p.
    "bestbuy": {
        "domain": "bestbuy.com",
        "search_url": "https://www.{domain}/site/searchpage.jsp?st={query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "a[href*='/site/'], .sku-item",
        "keyterms": {
            "Electronics": ["wireless headphones", "4k tv", "gaming laptop", "smart watch",
                            "bluetooth speaker", "webcam", "soundbar"],
            "Computers": ["mechanical keyboard", "gaming mouse", "external ssd", "monitor",
                          "usb hub", "wifi router"],
            "Gaming": ["game controller", "gaming headset", "graphics card"],
        },
    },
    # Etsy. Handmade/vintage marketplace; listing pages ship schema.org Product JSON-LD.
    # RELIABLE (lighter wall than the big-box stores). source_id is the numeric /listing/<id>/.
    "etsy": {
        "domain": "etsy.com",
        "search_url": "https://www.{domain}/search?q={query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "a[href*='/listing/']",
        "keyterms": {
            "Jewelry": ["silver necklace", "beaded bracelet", "stud earrings", "gemstone ring"],
            "Home": ["macrame wall hanging", "ceramic mug", "wooden cutting board", "scented candle"],
            "Art": ["watercolor print", "canvas wall art", "enamel pin", "sticker pack"],
            "Craft": ["knitting pattern", "yarn skein", "leather journal", "embroidery kit"],
        },
    },
    # Wayfair. Furniture/home; product pages ship schema.org Product JSON-LD. RELIABLE for
    # detail; SRP is JS-rendered. source_id recovered from the /pdp/...-<sku>.html link tail.
    "wayfair": {
        "domain": "wayfair.com",
        "search_url": "https://www.{domain}/keyword.php?keyword={query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "a[href*='/pdp/'], [data-enzyme-id='ProductCard']",
        "keyterms": {
            "Furniture": ["accent chair", "coffee table", "bookshelf", "bar stool", "nightstand"],
            "Decor": ["area rug", "wall mirror", "table lamp", "throw pillow", "wall art"],
            "Bedroom": ["bed frame", "dresser", "mattress", "headboard"],
            "Outdoor": ["patio set", "fire pit", "outdoor rug", "garden bench"],
        },
    },
    # Home Depot. Big-box hardware; product pages ship schema.org Product JSON-LD. RELIABLE for
    # detail; SRP is JS-rendered. source_id is the numeric /p/.../<id> (Internet number).
    "homedepot": {
        "domain": "homedepot.com",
        "search_url": "https://www.{domain}/s/{query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "a[href*='/p/'], [data-testid='product-pod']",
        "keyterms": {
            "Tools": ["cordless drill", "impact driver", "tool box", "tape measure",
                      "circular saw", "wrench set"],
            "Hardware": ["door knob", "cabinet handles", "led shop light", "extension cord"],
            "Garden": ["garden hose", "potting soil", "lawn mower", "pruning shears"],
            "Paint": ["paint roller", "painters tape", "spray paint", "paint brush set"],
        },
    },
    # Lowe's. Big-box hardware; product pages ship schema.org Product JSON-LD. RELIABLE for
    # detail; SRP is JS-rendered. source_id recovered from the /pd/...-<id> link tail.
    "lowes": {
        "domain": "lowes.com",
        "search_url": "https://www.{domain}/search?searchTerm={query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "a[href*='/pd/'], [data-selector='splp-prd-lnk']",
        "keyterms": {
            "Tools": ["cordless drill", "shop vac", "tool cabinet", "level", "socket set"],
            "Hardware": ["led light bulb", "smart thermostat", "door lock", "power strip"],
            "Garden": ["leaf blower", "garden soil", "watering can", "plant pots"],
            "Appliances": ["microwave", "mini fridge", "space heater", "dehumidifier"],
        },
    },
    # B&H Photo. Photo/video/pro-audio retailer; product pages ship schema.org Product JSON-LD.
    # RELIABLE (lighter wall than the big-box stores). source_id recovered from the link tail.
    "bhphoto": {
        "domain": "bhphotovideo.com",
        "search_url": "https://www.{domain}/c/search?Ntt={query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "[data-selenium='miniProductPage'], a[href*='/c/product/']",
        "keyterms": {
            "Camera": ["mirrorless camera", "camera tripod", "camera lens", "sd card",
                       "camera bag", "external flash"],
            "Audio": ["studio headphones", "usb microphone", "audio interface", "midi keyboard"],
            "Video": ["led video light", "capture card", "gimbal stabilizer", "hdmi capture"],
            "Computers": ["portable ssd", "usb c dock", "monitor", "mechanical keyboard"],
        },
    },
    # Chewy. Pet supplies; product pages ship schema.org Product JSON-LD. RELIABLE (lighter
    # wall). source_id recovered from the /dp/<id> link tail.
    "chewy": {
        "domain": "chewy.com",
        "search_url": "https://www.{domain}/s?query={query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "a[href*='/dp/'], article[data-testid='product-card']",
        "keyterms": {
            "Dog": ["dog food", "dog bed", "dog leash", "chew toy", "dog crate"],
            "Cat": ["cat litter", "cat tree", "cat food", "scratching post"],
            "Aquarium": ["fish tank", "aquarium filter", "fish food", "water conditioner"],
            "SmallPet": ["hamster cage", "bird cage", "rabbit hutch", "guinea pig food"],
        },
    },
    # IKEA. Furniture/home; product pages ship schema.org Product JSON-LD. RELIABLE for detail;
    # SRP is JS-rendered. source_id recovered from the /p/...-<digits>/ link tail.
    "ikea": {
        "domain": "ikea.com",
        "search_url": "https://www.{domain}/us/en/search/?q={query}",
        "selectors": None,
        "engine": "browser",
        "wait_selector": "a[href*='/p/'], .plp-product-list__products",
        "keyterms": {
            "Furniture": ["bookshelf", "desk", "dining chair", "wardrobe", "tv stand"],
            "Storage": ["storage box", "shoe rack", "drawer unit", "shelf unit"],
            "Lighting": ["floor lamp", "table lamp", "led bulb", "pendant lamp"],
            "Kitchen": ["dish rack", "food container", "cutlery set", "frying pan"],
        },
    },
}


class _SeenStore:
    """Persistent ``source_id -> last-seen epoch`` map for detail-page caching.

    Mirrors the harvest-store pattern (one atomic JSON file on the durable /data volume).
    A ``source_id`` "seen" within ``ttl_secs`` is a cache hit: its detail page was enriched
    recently, so the next sweep keeps the card-level fields and skips the slow, block-prone
    per-item detail fetch. Entries older than the TTL are pruned on load so the file can't
    grow without bound, and stale prices/details still get re-fetched eventually.
    """

    def __init__(self, path: str, *, ttl_secs: float = 7 * 24 * 3600, clock=None) -> None:
        self.path = path
        self.ttl_secs = float(ttl_secs)
        self._clock = clock or time.time
        self.seen: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            now = self._clock()
            self.seen = {
                str(k): float(v) for k, v in dict(data.get("seen", {})).items()
                if now - float(v) < self.ttl_secs        # prune expired on load
            }
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            pass  # first run / unreadable -> start empty

    def fresh_ids(self) -> set[str]:
        """source_ids still within the TTL — their detail page need not be re-fetched."""
        now = self._clock()
        return {sid for sid, ts in self.seen.items() if now - ts < self.ttl_secs}

    def mark(self, source_ids) -> None:
        now = self._clock()
        for sid in source_ids:
            if sid:
                self.seen[str(sid)] = now

    def save(self) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seen": self.seen}, f)
            os.replace(tmp, self.path)  # atomic
        except OSError as exc:  # noqa: BLE001
            log.warning("seen store save failed (%s): %s", self.path, exc)


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
        detail_cache: bool = False,
        detail_cache_path: str | None = None,
        detail_cache_ttl_secs: float = 7 * 24 * 3600,
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
        # Detail-page cache: skip re-fetching detail for source_ids seen within the TTL.
        # Only meaningful when with_description is on (that's what triggers the per-item fetch).
        self.detail_cache = bool(detail_cache)
        self._seen = (
            _SeenStore(
                detail_cache_path or f"/data/{self.profile_name}_seen.json",
                ttl_secs=detail_cache_ttl_secs,
            )
            if self.detail_cache else None
        )

    def _child(self, term: str, category: str | None,
               skip_detail_ids: set | None = None) -> AmazonSearchPollable:
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
            skip_detail_ids=skip_detail_ids,
        )

    def _sample(self) -> list[tuple[str, str]]:
        pairs = [(t, cat) for cat, terms in self.categories.items() for t in terms]
        if not pairs:
            return []
        k = min(self.terms_per_poll, len(pairs))
        return self._rng.sample(pairs, k)

    async def poll(self, prev: PollResult | None) -> PollResult:
        pairs = self._sample()
        # Detail-page cache: ids enriched within the TTL skip the per-item detail fetch.
        skip_ids = self._seen.fresh_ids() if self._seen is not None else None
        if skip_ids:
            log.info("marketplace[%s] detail-cache: %d ids fresh (skip detail fetch)",
                     self.profile_name, len(skip_ids))
        log.info("marketplace[%s] sweep: %s", self.profile_name, [t for t, _ in pairs])
        # Term/site fan-out runs concurrently (asyncio.gather) — one slow/blocked site
        # never serializes the rest of the sweep.
        children = [self._child(term, cat, skip_detail_ids=skip_ids) for term, cat in pairs]
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
        # Record the ids we surfaced so the next sweep can skip their detail fetch, then
        # persist (atomic JSON on the durable volume, mirroring the harvest store).
        if self._seen is not None:
            self._seen.mark(seen)
            self._seen.save()
        return PollResult(
            ok=True,
            changed=decide_changed(fingerprint(combined), prev),
            fingerprint=fingerprint(combined),
            payload=combined,
        )
