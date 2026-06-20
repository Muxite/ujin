"""Marketplace product extraction — turn a listing/search page into products.

A reusable companion to :mod:`ujin.extract.structured` /
:mod:`ujin.extract.article`: where those return article bodies or raw
structured blobs, this returns normalized **products** (title, image, price in
minor units, currency) ready for a sink or an ingest API.

Two layers, tried in order and merged:

1. **Structured data** (preferred, site-agnostic): JSON-LD ``@type==Product`` and
   OpenGraph ``product:*`` tags via :func:`ujin.extract.structured.extract_structured`.
   Works on any schema.org-compliant store.
2. **CSS fallback** (``selectolax``): search-result cards. Selectors default to
   Amazon's ``s-search-result`` grid but are overridable, so the same extractor
   serves other marketplaces by passing ``selectors=``.

Prices are normalized to integer **minor units** (cents) so callers never deal
with floats: ``"$49.99" -> 4999``.
"""
from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .structured import extract_structured

__all__ = [
    "Product",
    "extract_products",
    "extract_product_detail",
    "price_to_cents",
    "clean_product_name",
    "SCRAPE_VERSION_CARD",
    "SCRAPE_VERSION_DETAIL",
]

# Per-item scrape coverage, stamped on every Product so the DB records *how much* info a
# row carries. Bump DETAIL when the detail-page extractor learns a new field; bump CARD if
# the search-card path ever gains fields. Stored as `oon_listings.scrape_version`.
SCRAPE_VERSION_CARD = 1     # search-result card: title, one image, price, currency, url
SCRAPE_VERSION_DETAIL = 2   # detail page: + brand, rating, reviews, image gallery, specs, variant


@dataclass
class Product:
    """One normalized marketplace item."""

    source: str
    source_id: str | None
    title: str
    image_url: str
    price_cents: int
    currency: str = "USD"
    category: str | None = None
    url: str | None = None
    description: str | None = None   # filled from the detail page when requested
    # Rich detail-page fields (empty/None until enriched by extract_product_detail).
    brand: str | None = None
    rating: float | None = None             # 0–5 stars
    review_count: int | None = None
    images: list[str] = field(default_factory=list)   # gallery (large URLs), image_url first
    specs: dict[str, str] = field(default_factory=dict)  # key fact table (capacity, interface, …)
    variant: str | None = None              # selected variant label, e.g. "990 PRO HS · 2TB"
    scrape_version: int = SCRAPE_VERSION_CARD


# Where to find a product's description on a detail page, per source. Override via
# extract_description(..., selectors=...).
_DESC_SELECTORS: dict[str, dict] = {
    "amazon": {"bullets": "#feature-bullets li span.a-list-item", "block": "#productDescription"},
    "newegg": {"bullets": ".product-bullets li", "block": ".product-overview-content"},
}


def extract_description(
    html: str, *, source: str = "amazon", selectors: dict | None = None, max_chars: int = 600,
) -> str | None:
    """Pull a short product description from a detail-page HTML (bullets > block > meta).

    :returns: A cleaned single-line description (<= max_chars), or None if nothing usable.
    """
    try:
        from selectolax.parser import HTMLParser
    except ImportError:  # pragma: no cover
        return None
    tree = HTMLParser(html)
    sel = selectors or _DESC_SELECTORS.get(source, _DESC_SELECTORS["amazon"])
    parts: list[str] = []
    for node in tree.css(sel.get("bullets") or ""):
        t = (node.text() or "").strip()
        if t:
            parts.append(t)
    if not parts and sel.get("block"):
        node = tree.css_first(sel["block"])
        if node and (node.text() or "").strip():
            parts.append(node.text().strip())
    desc = " · ".join(parts).strip()
    if not desc:
        for q in ('meta[name="description"]', 'meta[property="og:description"]'):
            m = tree.css_first(q)
            content = m.attributes.get("content") if m else None
            if content and content.strip():
                desc = content.strip()
                break
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc[:max_chars] or None


# Default CSS selectors for an Amazon search-results grid. Override via
# ``extract_products(..., selectors={...})`` for another marketplace.
_AMAZON_SELECTORS = {
    "card": "div[data-component-type='s-search-result']",
    "id_attr": "data-asin",
    # Several layouts put the brand in one element and the full title in another
    # ("Logitech" vs "Logitech MX Master 3S ..."). Gather all candidates across
    # these selectors and keep the longest — the product title, not the brand.
    # `[data-cy='title-recipe']` is the current grid's title wrapper; the bare h2
    # selectors stay as fallbacks for older/drifted layouts.
    "title": ("[data-cy='title-recipe'] h2 span", "h2 a span", "h2 span", "h2"),
    # Lazy-loaded grids serve a placeholder src and stash the real URL in data-src/
    # srcset — `_from_cards` reads those too; the selectors just need to match the <img>.
    "image": (".s-image", "img.s-image", "img"),
    "price": (".a-price .a-offscreen", ".a-price span.a-offscreen"),
    "link": ("a.a-link-normal.s-no-outline", "a.a-link-normal"),
}


# Markers Amazon uses to flag a paid placement. Sponsored cards are usually a
# poor match for the query (competitors, accessories), so callers can skip them.
_SPONSORED_SELECTORS = (
    ".puis-sponsored-label-text",
    ".s-sponsored-label-text",
    ".s-sponsored-label-info-icon",
    "[data-component-type='sp-sponsored-result']",
)


def _is_sponsored(card) -> bool:
    return any(card.css_first(sel) is not None for sel in _SPONSORED_SELECTORS)


# Placeholder/promo cards some marketplaces inject into the results grid (eBay's first
# card is a "Shop on eBay" promo with a $0.99 price and no real product). They have a
# generic title, so skip by exact (case-folded) title match.
_JUNK_TITLES: frozenset[str] = frozenset({
    "shop on ebay", "new listing",
})


def _is_junk_title(title: str | None) -> bool:
    return bool(title) and title.strip().casefold() in _JUNK_TITLES


# Badge text some marketplaces glue to the front of a card title (eBay prepends a
# "New Listing" / "Sponsored" badge inside the title span, e.g. "New ListingApple ...").
_TITLE_BADGE_RE = re.compile(r"^(?:new listing|sponsored|top rated plus)\s*", re.I)


def _best_title(card, title_selectors) -> str | None:
    """Pick the longest non-empty title candidate (avoids brand-only spans)."""
    candidates: list[str] = []
    for sel in _as_selectors(title_selectors):
        for node in card.css(sel):
            text = _TITLE_BADGE_RE.sub("", node.text(strip=True))
            if text:
                candidates.append(text)
    return max(candidates, key=len) if candidates else None


def _as_selectors(value) -> tuple[str, ...]:
    """Normalize a selector config (str or iterable of str) to a tuple of strings.

    Site profiles may list several fallback selectors per field (e.g. Newegg's
    ``"price": (".price-current", ".price-current strong")``). ``selectolax``'s
    ``css``/``css_first`` only take a single string, so every selector lookup goes
    through this — passing a tuple straight to ``css_first`` would raise a TypeError
    and silently zero out the whole scrape.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(s) for s in value if s)


def _first_node(card, selectors):
    """First node matching any of ``selectors`` (str or tuple), in order; else None."""
    for sel in _as_selectors(selectors):
        node = card.css_first(sel)
        if node is not None:
            return node
    return None


def _first_srcset(srcset: str | None) -> str | None:
    """First URL out of a ``srcset`` attribute (``"a.jpg 1x, b.jpg 2x"`` -> ``a.jpg``)."""
    if not srcset:
        return None
    first = srcset.split(",", 1)[0].strip()
    return first.split()[0] if first else None


def _card_image(card, selectors) -> str:
    """Best image URL from a card across all image selectors.

    Robust to layout drift and lazy-loading: tries each selector's matches in order and
    returns the first ``src``/``data-src``/``srcset`` that's a real http(s) URL (a leading
    placeholder ``<img>`` with an empty/data: src no longer kills the card)."""
    for sel in _as_selectors(selectors):
        for node in card.css(sel):
            for attr in ("src", "data-src", "data-image-src"):
                u = node.attributes.get(attr) or ""
                if u.startswith("http"):
                    return u
            u = _first_srcset(node.attributes.get("srcset")) or ""
            if u.startswith("http"):
                return u
    return ""


# Per-source patterns that recover a stable product id from a card's link when the
# card itself carries no id attribute (e.g. Newegg's `.item-cell` has no data-id).
_HREF_ID_PATTERNS: dict[str, re.Pattern] = {
    "newegg": re.compile(r"/p/([A-Za-z0-9]+)"),          # .../p/N82E16820982185
    "amazon": re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})"),
    "ebay": re.compile(r"/itm/(\d+)"),                   # .../itm/123456789012 (legacy id)
    # Walmart product pages are /ip/<slug>/<numeric-id> or /ip/<numeric-id>.
    "walmart": re.compile(r"/ip/(?:.*/)?(\d+)"),
    # JSON-LD-first stores: recover a stable numeric/slug id from the canonical product link.
    "aliexpress": re.compile(r"/item/(\d+)\.html"),       # .../item/100500.../...html
    "target": re.compile(r"/A-(\d+)"),                    # .../p/<slug>/-/A-12345678
    "bestbuy": re.compile(r"/(\d+)\.p"),                  # .../site/<slug>/6500000.p
    "etsy": re.compile(r"/listing/(\d+)"),                # .../listing/123456789/<slug>
    "wayfair": re.compile(r"-([a-z]{2,5}\d{3,})\.html", re.I),  # .../pdp/<slug>-ABCD1234.html
    "homedepot": re.compile(r"/p/(?:.*/)?(\d{8,})"),      # .../p/<slug>/312345678
    "lowes": re.compile(r"/pd/(?:.*/)?(\d{6,})"),         # .../pd/<slug>/1000123456
    "bhphoto": re.compile(r"/c/product/(\d+)"),           # .../c/product/1234567-REG/<slug>
    "chewy": re.compile(r"/dp/(\d+)"),                    # .../dp/123456
    "ikea": re.compile(r"-(\d{8,})/"),                    # .../p/<slug>-s09390333/  (digits run)
}


def _id_from_href(href: str | None, source: str) -> str | None:
    """Best-effort stable source_id from a product link; falls back to the path."""
    if not href:
        return None
    pat = _HREF_ID_PATTERNS.get(source)
    if pat:
        m = pat.search(href)
        if m:
            return m.group(1)
    # Generic fallback: the last non-empty path segment (drops the query string).
    path = href.split("?", 1)[0].rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    return tail or None


_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR"}
# Thousands separators (comma / thin space between digit groups) are stripped
# before matching, so a bare integer ("1299") isn't truncated at the first group.
_THOUSANDS_RE = re.compile(r"(?<=\d)[,\s](?=\d{3}\b)")
_PRICE_RE = re.compile(r"\d+(?:\.\d+)?")


def price_to_cents(text: str | float | int | None) -> int | None:
    """Parse a price into integer minor units (cents). ``"$1,299.00" -> 129900``.

    Returns ``None`` when no numeric price is present. Floats/ints are treated as
    major units (dollars), so ``49.99 -> 4999``.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(round(float(text) * 100))
    cleaned = _THOUSANDS_RE.sub("", str(text))
    m = _PRICE_RE.search(cleaned)
    if not m:
        return None
    try:
        return int(round(float(m.group(0)) * 100))
    except ValueError:
        return None


# Delimiters that usually separate the product name from the marketing tail of
# a marketplace title ("Acme Widget, 3-pack, BPA-free ..." -> "Acme Widget").
_NAME_DELIMS = (",", " - ", " – ", " — ", " | ", ": ", "; ", " • ", " w/ ", " with ")
_BRACKETS_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")


def clean_product_name(title: str, *, max_words: int = 8) -> str:
    """Heuristically shorten a verbose marketplace title to a product name.

    Non-LLM: collapse whitespace, cut at the earliest "marketing" delimiter,
    drop bracketed asides, and cap word count. ``"WH-1000XM5 Headphones,
    30-Hour Battery, ..." -> "WH-1000XM5 Headphones"``.
    """
    if not title:
        return title
    original = " ".join(str(title).split())
    cut = len(original)
    for d in _NAME_DELIMS:
        i = original.find(d)
        if 0 < i < cut:
            cut = i
    t = _BRACKETS_RE.sub("", original[:cut])
    t = " ".join(t.split())
    words = t.split(" ")
    if len(words) > max_words:
        t = " ".join(words[:max_words])
    t = t.strip(" -–—|,:;•")
    # Fall back to a word-capped original if cutting left nothing usable.
    return t or " ".join(original.split()[:max_words])


def _currency_from_text(text: str, default: str = "USD") -> str:
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in text:
            return code
    return default


def _first(value):
    """JSON-LD fields are often a value OR a list of values; take the first."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _from_jsonld(blocks: list, *, source: str, base_url: str) -> list[Product]:
    out: list[Product] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        types = block.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types:
            continue
        offers = _first(block.get("offers")) or {}
        if not isinstance(offers, dict):
            offers = {}
        cents = price_to_cents(offers.get("price"))
        if cents is None or cents <= 0:
            continue
        image = _first(block.get("image"))
        if isinstance(image, dict):
            image = image.get("url")
        title = block.get("name")
        if not title or not image:
            continue
        out.append(Product(
            source=source,
            source_id=block.get("sku") or block.get("mpn") or block.get("gtin13"),
            title=str(title).strip(),
            image_url=urljoin(base_url, str(image)),
            price_cents=cents,
            currency=offers.get("priceCurrency") or "USD",
            url=urljoin(base_url, str(block.get("url") or base_url)),
        ))
    return out


def _from_cards(
    html: str, *, source: str, base_url: str, selectors: dict, skip_sponsored: bool = True
) -> list[Product]:
    try:
        from selectolax.parser import HTMLParser
    except ImportError:  # pragma: no cover - web extra missing
        return []
    tree = HTMLParser(html)
    out: list[Product] = []
    for card in tree.css(selectors["card"]):
        if skip_sponsored and _is_sponsored(card):
            continue
        title = _best_title(card, selectors["title"])
        price_node = _first_node(card, selectors["price"])
        if not (title and price_node) or _is_junk_title(title):
            continue
        price_text = price_node.text(strip=True)
        cents = price_to_cents(price_text)
        if cents is None or cents <= 0:
            continue
        image_url = _card_image(card, selectors["image"])
        if not image_url:
            continue
        source_id = card.attributes.get(selectors["id_attr"]) or None
        link_node = _first_node(card, selectors["link"])
        href = (link_node.attributes.get("href") if link_node else None) or ""
        if source_id is None:
            # No id attribute on the card (e.g. Newegg) — recover one from the link.
            source_id = _id_from_href(href, source)
        if source == "amazon" and source_id:
            # Canonical product URL — drop the noisy search-ref query string.
            url = f"https://www.amazon.com/dp/{source_id}"
        else:
            url = urljoin(base_url, href) if href else None
        out.append(Product(
            source=source,
            source_id=source_id,
            title=title,
            image_url=urljoin(base_url, image_url),
            price_cents=cents,
            currency=_currency_from_text(price_text),
            url=url,
        ))
    return out


def extract_products(
    html: str,
    base_url: str,
    *,
    source: str = "amazon",
    selectors: dict | None = None,
    skip_sponsored: bool = True,
) -> list[Product]:
    """Extract normalized products from a marketplace page.

    :param html: Rendered page HTML.
    :param base_url: Page URL, used to resolve relative image/product links.
    :param source: Value stamped on each product's ``source`` field.
    :param selectors: CSS selector overrides for the card fallback (defaults to
        Amazon search-grid selectors).
    :param skip_sponsored: Drop paid-placement cards (usually off-target).
    :returns: List of :class:`Product` (deduped by ``source_id`` when present).
    """
    products = _from_jsonld(
        extract_structured(html).get("jsonld", []), source=source, base_url=base_url
    )
    products += _from_cards(
        html, source=source, base_url=base_url,
        selectors={**_AMAZON_SELECTORS, **(selectors or {})},
        skip_sponsored=skip_sponsored,
    )
    # Dedupe: prefer the first occurrence of each source_id; keep id-less items.
    seen: set[str] = set()
    deduped: list[Product] = []
    for p in products:
        if p.source_id:
            if p.source_id in seen:
                continue
            seen.add(p.source_id)
        deduped.append(p)
    return deduped


# ── Detail-page extraction (rich) ──────────────────────────────────────────────
# Selectors for a single product detail page. Amazon defaults; override per source via
# extract_product_detail(..., selectors=...) to support another marketplace.
_AMAZON_DETAIL = {
    "title": "#productTitle",
    "byline": "#bylineInfo",
    "rating": "#acrPopover",                      # title attr: "4.5 out of 5 stars"
    "review_count": "#acrCustomerReviewText",     # "(4,994)"
    "price": (
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "#price_inside_buybox",
        ".a-price .a-offscreen",
    ),
    "overview": "#productOverview_feature_div tr",
    "detail_bullets": "#detailBullets_feature_div li",
    "variant": "span.inline-twister-dim-title-value.a-text-bold",
}

# Amazon prices rarely carry an explicit currency on the detail page, so infer it from the
# storefront TLD ("amazon.ca" -> CAD). Falls back to the price symbol, then USD.
_CURRENCY_BY_TLD = {
    "ca": "CAD", "com": "USD", "co.uk": "GBP", "de": "EUR", "fr": "EUR", "es": "EUR",
    "it": "EUR", "nl": "EUR", "co.jp": "JPY", "in": "INR", "com.au": "AUD", "com.mx": "MXN",
}


def _currency_from_url(url: str, price_text: str = "") -> str:
    m = re.search(r"amazon\.([a-z.]+?)(?:/|$)", url or "")
    if m and m.group(1) in _CURRENCY_BY_TLD:
        return _CURRENCY_BY_TLD[m.group(1)]
    return _currency_from_text(price_text)

# colorImages gallery objects look like {"hiRes":"URL"|null,"thumb":"URL","large":"URL",...}.
# Match that exact key run so we never pick up the nested `"main":{...}` map.
_GALLERY_RE = re.compile(
    r'"hiRes":(?:"(https://[^"]+)"|null),"thumb":"https://[^"]+","large":"(https://[^"]+)"'
)
_DIGITS_RE = re.compile(r"[\d,]+")
_RATING_RE = re.compile(r"([\d.]+)\s+out of\s+5")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", _html.unescape(text or "")).strip()


def _brand_from_byline(text: str) -> str | None:
    """'Visit the Samsung Store' / 'Brand: Samsung' -> 'Samsung'."""
    t = _clean(text)
    t = re.sub(r"^(visit the|brand:?)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+store$", "", t, flags=re.I)
    return t or None


def _extract_gallery(html: str, *, limit: int = 10) -> list[str]:
    """Ordered, deduped gallery image URLs from the `colorImages` JS var (hiRes > large)."""
    out: list[str] = []
    seen: set[str] = set()
    for hires, large in _GALLERY_RE.findall(html):
        url = hires or large
        if url and url not in seen:
            seen.add(url)
            out.append(url)
            if len(out) >= limit:
                break
    return out


def _extract_specs(tree, sel: dict, *, limit: int = 14) -> dict[str, str]:
    """Key/value spec table from the product-overview grid, then detail bullets."""
    specs: dict[str, str] = {}
    for row in tree.css(sel["overview"]):
        cells = row.css("td, th")
        if len(cells) >= 2:
            key, val = _clean(cells[0].text()), _clean(cells[1].text())
            # Skip widget/template debris (long blobs, leftover braces) — real facts are short.
            if key and val and key not in specs and len(val) <= 80 and "{" not in val:
                specs[key] = val
    # Detail bullets read "Key ‏ : ‎ Value" inside one <li>, padded with bidi marks
    # (U+200E/U+200F) and spaces around the colon — strip those when splitting.
    for li in tree.css(sel["detail_bullets"]):
        parts = re.split(r"[\s‎‏]*[:：][\s‎‏]*", _clean(li.text()), maxsplit=1)
        if len(parts) == 2:
            key, val = _clean(parts[0]), _clean(parts[1])
            if key and val and key not in specs and len(key) < 40:
                specs[key] = val
        if len(specs) >= limit:
            break
    return dict(list(specs.items())[:limit])


def _selected_variant(tree, sel: dict) -> str | None:
    """Selected variant label from the twister's bold dimension values ('990 PRO HS · 2TB')."""
    vals: list[str] = []
    for node in tree.css(sel["variant"]):
        t = _clean(node.text())
        if t and t not in vals:
            vals.append(t)
    return " · ".join(vals) if vals else None


def _detail_from_jsonld(
    html: str, url: str, *, source: str, max_chars: int = 600,
) -> Product | None:
    """Build a rich :class:`Product` from a detail page's schema.org ``Product`` JSON-LD.

    Site-agnostic — any store that ships a ``@type==Product`` block (Newegg, Shopify,
    most modern carts) yields brand/rating/reviews/price/image/specs without per-site CSS.
    Used as the primary extractor for non-Amazon sources and as an Amazon fallback when the
    DOM parse fails (e.g. layout drift / a partial bot wall). Returns ``None`` if there is no
    usable Product block with a positive price.
    """
    blocks = extract_structured(html).get("jsonld", [])
    block: dict | None = None
    for b in blocks:
        if not isinstance(b, dict):
            continue
        types = b.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" in types:
            block = b
            break
    if block is None:
        return None

    title = _clean(block.get("name"))
    if not title:
        return None

    offers = _first(block.get("offers")) or {}
    if not isinstance(offers, dict):
        offers = {}
    cents = price_to_cents(offers.get("price"))
    if cents is None or cents <= 0:
        return None
    currency = str(offers.get("priceCurrency") or "").strip() or _currency_from_url(url)

    image = _first(block.get("image"))
    if isinstance(image, dict):
        image = image.get("url")
    images: list[str] = []
    raw_images = block.get("image")
    for raw in (raw_images if isinstance(raw_images, list) else [raw_images]):
        u = raw.get("url") if isinstance(raw, dict) else raw
        u = _http_url_str(u, url)
        if u and u not in images:
            images.append(u)
    image_url = _http_url_str(image, url) or (images[0] if images else "")
    if not image_url:
        og = extract_structured(html).get("opengraph", {})
        image_url = _http_url_str(og.get("og:image"), url) or ""
    if not image_url:
        return None

    brand = block.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    brand = _clean(brand) or None

    rating = review_count = None
    agg = block.get("aggregateRating")
    if isinstance(agg, dict):
        rating = _clean_float(agg.get("ratingValue"))
        review_count = _clean_pos_int(agg.get("reviewCount") or agg.get("ratingCount"))

    # Specs: schema.org scatters facts as flat scalar props (Model, weight, …). Keep short
    # string pairs, skip schema plumbing and nested objects.
    _SKIP_KEYS = {
        "@context", "@type", "@id", "name", "description", "image", "offers", "brand",
        "aggregateRating", "review", "sku", "mpn", "gtin", "gtin12", "gtin13", "gtin14",
        "url", "itemCondition", "category", "productID",
    }
    specs: dict[str, str] = {}
    for k, v in block.items():
        if k in _SKIP_KEYS or isinstance(v, (dict, list)):
            continue
        key, val = _clean(k)[:48], _clean(v)[:80]
        if key and val and "{" not in val:
            specs[key] = val
        if len(specs) >= 14:
            break

    variant = _clean(block.get("mpn") or block.get("Model")) or None
    source_id = _clean(block.get("sku") or block.get("mpn")) or _id_from_href(url, source)

    return Product(
        source=source,
        source_id=source_id,
        title=title,
        image_url=image_url,
        price_cents=cents,
        currency=currency,
        url=url,
        description=extract_description(html, source=source, max_chars=max_chars)
        or (_clean(block.get("description"))[:max_chars] or None),
        brand=brand,
        rating=rating,
        review_count=review_count,
        images=images[:10],
        specs=specs,
        variant=variant,
        scrape_version=SCRAPE_VERSION_DETAIL,
    )


def _http_url_str(value, base_url: str) -> str | None:
    s = str(value or "").strip()
    if s.startswith("//"):
        s = "https:" + s
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return urljoin(base_url, s) if s else None


def _clean_float(raw) -> float | None:
    try:
        return round(float(raw), 1)
    except (TypeError, ValueError):
        return None


def _clean_pos_int(raw) -> int | None:
    m = _DIGITS_RE.search(str(raw or ""))
    if not m:
        return None
    try:
        n = int(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return n if n >= 0 else None


def extract_product_detail(
    html: str, url: str, *, source: str = "amazon", selectors: dict | None = None,
    max_chars: int = 600,
) -> Product | None:
    """Extract one rich :class:`Product` from a marketplace **detail** page.

    Pulls title, brand, rating, review count, an image gallery, a key-fact spec table,
    the selected variant, and a short description — everything a price-guesser needs to
    judge value (the price itself is captured for scoring, never shown). Returns ``None``
    when the page has no usable title+price (e.g. a bot/captcha wall).

    Dispatches on ``source``: AliExpress parses its ``window.runParams`` JSON; Amazon (and
    Amazon-shaped stores) parse the DOM, falling back to schema.org ``Product`` JSON-LD when
    the DOM parse comes up empty; every other source uses the JSON-LD path directly. The
    result is stamped ``scrape_version = SCRAPE_VERSION_DETAIL`` so the DB records the richer
    coverage versus the search-card path (:data:`SCRAPE_VERSION_CARD`).
    """
    if source == "aliexpress":
        return _aliexpress_detail(html, url, max_chars=max_chars)
    # Non-Amazon sources (Newegg, generic schema.org stores) have no Amazon DOM — read the
    # site-agnostic JSON-LD Product block straight away.
    if source != "amazon" and not selectors:
        return _detail_from_jsonld(html, url, source=source, max_chars=max_chars)
    try:
        from selectolax.parser import HTMLParser
    except ImportError:  # pragma: no cover - web extra missing
        return None
    # Gallery is parsed from the raw `colorImages` JS var below, so strip <script>/<style>
    # from the tree first — that keeps their text out of `.text()` (e.g. a truncate-widget
    # script embedded in a spec <td>) without losing the image data.
    tree = HTMLParser(html)
    tree.strip_tags(["script", "style", "noscript", "template"])
    sel = {**_AMAZON_DETAIL, **(selectors or {})}

    title_node = tree.css_first(sel["title"])
    title = _clean(title_node.text()) if title_node else None
    if not title:
        # DOM title missing (layout drift / partial bot wall) — try schema.org JSON-LD,
        # which Amazon ships on most detail pages, before giving up.
        return _detail_from_jsonld(html, url, source=source, max_chars=max_chars)

    # Price: first non-empty offscreen value across the buy-box candidates.
    cents: int | None = None
    price_text = ""
    for q in _as_selectors(sel["price"]):
        node = tree.css_first(q)
        txt = _clean(node.text()) if node else ""
        if txt:
            c = price_to_cents(txt)
            if c and c > 0:
                cents, price_text = c, txt
                break
    if cents is None:
        return _detail_from_jsonld(html, url, source=source, max_chars=max_chars)

    # Rating from the popover title attr ("4.5 out of 5 stars").
    rating: float | None = None
    rnode = tree.css_first(sel["rating"])
    if rnode:
        m = _RATING_RE.search(rnode.attributes.get("title") or "")
        if m:
            try:
                rating = float(m.group(1))
            except ValueError:
                rating = None

    review_count: int | None = None
    rc = tree.css_first(sel["review_count"])
    if rc:
        m = _DIGITS_RE.search(rc.text() or "")
        if m:
            review_count = int(m.group(0).replace(",", ""))

    byline = tree.css_first(sel["byline"])
    brand = _brand_from_byline(byline.text()) if byline else None

    images = _extract_gallery(html)
    image_url = urljoin(url, images[0]) if images else ""
    if not image_url:
        og = tree.css_first('meta[property="og:image"]')
        image_url = (og.attributes.get("content") if og else "") or ""
    if not image_url:
        return None

    source_id = None
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if m:
        source_id = m.group(1)

    return Product(
        source=source,
        source_id=source_id,
        title=title,
        image_url=image_url,
        price_cents=cents,
        currency=_currency_from_url(url, price_text),
        url=url,
        description=extract_description(html, source=source, max_chars=max_chars),
        brand=brand,
        rating=rating,
        review_count=review_count,
        images=[urljoin(url, i) for i in images],
        specs=_extract_specs(tree, sel),
        variant=_selected_variant(tree, sel),
        scrape_version=SCRAPE_VERSION_DETAIL,
    )


# ── AliExpress detail extraction (window.runParams JSON) ──────────────────────
# AliExpress renders the product from a big JSON blob, not server-side DOM. The data lives in
# `window.runParams = {...}` (older pages) or `_init_data_ = {...}` (newer), keyed by *Module
# objects (titleModule, priceModule, imageModule, skuModule, specsModule, …). Requires a real
# browser render upstream — a bare HTTP fetch returns an anti-bot shell with no modules.

def _balanced_json(text: str, start: int) -> str | None:
    """Return the JSON object substring starting at `text[start] == '{'`, brace-matched."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _aliexpress_modules(html: str) -> dict | None:
    """Locate and parse the runParams/_init_data_ blob, returning the *Module map (data)."""
    for marker in (r"window\.runParams\s*=", r"runParams\s*=", r"_init_data_\s*=", r"_INIT_DATA_\s*="):
        m = re.search(marker, html)
        if not m:
            continue
        brace = html.find("{", m.end())
        if brace < 0:
            continue
        blob = _balanced_json(html, brace)
        if not blob:
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict) and ("titleModule" in data or "priceModule" in data):
            return data
        # Newer/nested shapes: walk the tree and collect any *Module dicts we can find.
        found: dict = {}

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k.endswith("Module") and isinstance(v, dict):
                        found.setdefault(k, v)
                    walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)

        walk(obj)
        if "titleModule" in found or "priceModule" in found:
            return found
    return None


def _ali_price_cents(price_mod: dict) -> tuple[int | None, str]:
    """Canonical buy-box price (sale/activity price preferred), as (cents, currency)."""
    # Structured amounts first (currency is explicit), activity (sale) price beats list price.
    for key in ("minActivityAmount", "minAmount", "maxActivityAmount", "maxAmount"):
        amt = price_mod.get(key)
        if isinstance(amt, dict) and amt.get("value") is not None:
            cents = price_to_cents(amt["value"])
            if cents and cents > 0:
                return cents, str(amt.get("currency") or "USD")
    # Fall back to the formatted strings ("C $30.58") — currency code is then unknown.
    for key in ("formatedActivityPrice", "formatedPrice"):
        cents = price_to_cents(price_mod.get(key))
        if cents and cents > 0:
            return cents, _currency_from_text(str(price_mod.get(key) or ""))
    return None, "USD"


def _aliexpress_detail(html: str, url: str, *, max_chars: int = 600) -> Product | None:
    data = _aliexpress_modules(html)
    if not data:
        return None
    title_mod = data.get("titleModule") or {}
    price_mod = data.get("priceModule") or {}
    image_mod = data.get("imageModule") or {}
    specs_mod = data.get("specsModule") or {}
    sku_mod = data.get("skuModule") or {}

    title = _clean(title_mod.get("subject") or title_mod.get("title"))
    if not title:
        return None
    cents, currency = _ali_price_cents(price_mod)
    if cents is None:
        return None

    # Gallery: protocol-relative URLs -> https.
    images: list[str] = []
    for raw in (image_mod.get("imagePathList") or [])[:10]:
        u = str(raw or "")
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and u not in images:
            images.append(u)
    if not images:
        return None

    # Rating + review count from the feedback rating block.
    rating = review_count = None
    fb = title_mod.get("feedbackRating") or {}
    try:
        rating = round(float(fb.get("averageStar")), 1) if fb.get("averageStar") else None
    except (TypeError, ValueError):
        rating = None
    for k in ("totalValidNum", "totalValidNumStr", "evarageStarRate"):
        m = _DIGITS_RE.search(str(fb.get(k) or ""))
        if k in ("totalValidNum", "totalValidNumStr") and m:
            review_count = int(m.group(0).replace(",", ""))
            break

    # Specs: list of {attrName, attrValue}. Brand is one of them.
    specs: dict[str, str] = {}
    brand = None
    for prop in (specs_mod.get("props") or []):
        name, val = _clean(prop.get("attrName")), _clean(prop.get("attrValue"))
        if name and val:
            if name.lower() in ("brand name", "brand") and not brand:
                brand = val
            if name not in specs and len(val) <= 120:
                specs[name] = val
        if len(specs) >= 14:
            break

    # Variant: first display value of each SKU property ("1.8M Rod 1000 Reel" etc.).
    variant_parts: list[str] = []
    for prop in (sku_mod.get("productSKUPropertyList") or []):
        vals = prop.get("skuPropertyValues") or []
        if vals:
            v = _clean(vals[0].get("propertyValueDisplayName") or vals[0].get("propertyValueName"))
            if v:
                variant_parts.append(v)
    variant = " · ".join(variant_parts) if variant_parts else None

    source_id = None
    m = re.search(r"/item/(\d+)\.html", url)
    if m:
        source_id = m.group(1)

    return Product(
        source="aliexpress",
        source_id=source_id,
        title=title,
        image_url=images[0],
        price_cents=cents,
        currency=currency,
        url=url,
        description=(title[:max_chars] or None),  # AliExpress has no concise spec-bullet block
        brand=brand,
        rating=rating,
        review_count=review_count,
        images=images,
        specs=specs,
        variant=variant,
        scrape_version=SCRAPE_VERSION_DETAIL,
    )
