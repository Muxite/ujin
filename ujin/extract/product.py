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

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from .structured import extract_structured

__all__ = ["Product", "extract_products", "price_to_cents", "clean_product_name"]


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


# Default CSS selectors for an Amazon search-results grid. Override via
# ``extract_products(..., selectors={...})`` for another marketplace.
_AMAZON_SELECTORS = {
    "card": "div[data-component-type='s-search-result']",
    "id_attr": "data-asin",
    # Several layouts put the brand in one element and the full title in another
    # ("Logitech" vs "Logitech MX Master 3S ..."). Gather all candidates across
    # these selectors and keep the longest — the product title, not the brand.
    "title": ("h2 a span", "h2 span", "h2"),
    "image": ".s-image",
    "price": ".a-price .a-offscreen",
    "link": "a.a-link-normal",
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


def _best_title(card, title_selectors) -> str | None:
    """Pick the longest non-empty title candidate (avoids brand-only spans)."""
    if isinstance(title_selectors, str):
        title_selectors = (title_selectors,)
    candidates: list[str] = []
    for sel in title_selectors:
        for node in card.css(sel):
            text = node.text(strip=True)
            if text:
                candidates.append(text)
    return max(candidates, key=len) if candidates else None

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
        price_node = card.css_first(selectors["price"])
        image_node = card.css_first(selectors["image"])
        if not (title and price_node and image_node):
            continue
        price_text = price_node.text(strip=True)
        cents = price_to_cents(price_text)
        if cents is None or cents <= 0:
            continue
        image_url = image_node.attributes.get("src") or ""
        if not image_url:
            continue
        source_id = card.attributes.get(selectors["id_attr"]) or None
        if source == "amazon" and source_id:
            # Canonical product URL — drop the noisy search-ref query string.
            url = f"https://www.amazon.com/dp/{source_id}"
        else:
            link_node = card.css_first(selectors["link"])
            href = (link_node.attributes.get("href") if link_node else None) or ""
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
