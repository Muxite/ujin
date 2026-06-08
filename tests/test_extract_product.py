"""Offline tests for the marketplace product extractor (no network)."""
from __future__ import annotations

import pytest

from ujin.extract.product import (
    Product,
    clean_product_name,
    extract_products,
    price_to_cents,
)


@pytest.mark.parametrize("raw,expected", [
    ("737 Power Bank, 140W Max 3-Port Laptop Portable Charger, 24,000mAh", "737 Power Bank"),
    ("WH-1000XM5 Premium Noise Canceling Headphones, Auto NC Optimizer, 30-Hour",
     "WH-1000XM5 Premium Noise Canceling Headphones"),
    ("Apple AirPods Pro (2nd Generation) (Renewed)", "Apple AirPods Pro"),
    ("MX Master 3S - Performance Wireless Mouse with Ultra-Fast Scrolling", "MX Master 3S"),
    ("Instant Pot Duo 7-in-1 Electric Pressure Cooker | 6 Quart", "Instant Pot Duo 7-in-1 Electric Pressure Cooker"),
])
def test_clean_product_name(raw, expected):
    assert clean_product_name(raw) == expected


def test_clean_product_name_word_cap():
    long = "Alpha Beta Gamma Delta Epsilon Zeta Eta Theta Iota Kappa"
    assert clean_product_name(long, max_words=4) == "Alpha Beta Gamma Delta"


@pytest.mark.parametrize("text,expected", [
    ("$49.99", 4999),
    ("$1,299.00", 129900),
    ("1299", 129900),
    ("£12.50", 1250),
    ("", None),
    (None, None),
    (49.99, 4999),
    (10, 1000),
])
def test_price_to_cents(text, expected):
    assert price_to_cents(text) == expected


_JSONLD_HTML = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Test Widget","sku":"SKU123",
 "image":"https://img.example/widget.jpg",
 "offers":{"@type":"Offer","price":"24.99","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""


def test_extract_from_jsonld():
    products = extract_products(_JSONLD_HTML, "https://amazon.com/dp/SKU123")
    assert len(products) == 1
    p = products[0]
    assert isinstance(p, Product)
    assert p.title == "Test Widget"
    assert p.source_id == "SKU123"
    assert p.price_cents == 2499
    assert p.currency == "USD"
    assert p.image_url == "https://img.example/widget.jpg"


_CARD_HTML = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0ABC123">
  <h2><span>Anker 737 Power Bank</span></h2>
  <img class="s-image" src="https://m.media-amazon.com/images/I/abc.jpg"/>
  <span class="a-price"><span class="a-offscreen">$139.99</span></span>
  <a class="a-link-normal" href="/dp/B0ABC123"></a>
</div>
<div data-component-type="s-search-result" data-asin="B0NOPRICE">
  <h2><span>No Price Item</span></h2>
  <img class="s-image" src="https://m.media-amazon.com/images/I/xyz.jpg"/>
</div>
</body></html>
"""


def test_extract_from_cards_skips_priceless():
    products = extract_products(_CARD_HTML, "https://www.amazon.com/s?k=power+bank")
    # Only the card with a parseable price is returned.
    assert len(products) == 1
    p = products[0]
    assert p.source_id == "B0ABC123"
    assert p.title == "Anker 737 Power Bank"
    assert p.price_cents == 13999
    assert p.url == "https://www.amazon.com/dp/B0ABC123"
    assert p.image_url.endswith("abc.jpg")


def test_dedupe_by_source_id():
    products = extract_products(_CARD_HTML + _CARD_HTML,
                                "https://www.amazon.com/s?k=x")
    assert len(products) == 1  # duplicate ASIN collapsed


_BRAND_THEN_TITLE_HTML = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0BRANDED">
  <h2 class="a-size-base-plus"><span>Logitech</span></h2>
  <h2 class="a-size-medium"><a class="a-link-normal" href="/x">
    <span>Logitech MX Master 3S Wireless Performance Mouse</span></a></h2>
  <img class="s-image" src="https://m.media-amazon.com/images/I/abc.jpg"/>
  <span class="a-price"><span class="a-offscreen">$99.99</span></span>
</div>
</body></html>
"""


_SPONSORED_HTML = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0SPONSORED">
  <span class="puis-sponsored-label-text">Sponsored</span>
  <h2><a class="a-link-normal" href="/x"><span>Off-target Sponsored Mouse</span></a></h2>
  <img class="s-image" src="https://m.media-amazon.com/images/I/spon.jpg"/>
  <span class="a-price"><span class="a-offscreen">$19.99</span></span>
</div>
<div data-component-type="s-search-result" data-asin="B0ORGANIC">
  <h2><a class="a-link-normal" href="/y"><span>The Real Organic Product</span></a></h2>
  <img class="s-image" src="https://m.media-amazon.com/images/I/org.jpg"/>
  <span class="a-price"><span class="a-offscreen">$99.99</span></span>
</div>
</body></html>
"""


def test_skips_sponsored_by_default():
    products = extract_products(_SPONSORED_HTML, "https://www.amazon.com/s?k=mouse")
    assert [p.source_id for p in products] == ["B0ORGANIC"]
    # Opt out to keep sponsored cards.
    keep = extract_products(_SPONSORED_HTML, "https://www.amazon.com/s?k=mouse",
                            skip_sponsored=False)
    assert {p.source_id for p in keep} == {"B0SPONSORED", "B0ORGANIC"}


def test_title_picks_full_over_brand():
    products = extract_products(_BRAND_THEN_TITLE_HTML, "https://www.amazon.com/s?k=mouse")
    assert len(products) == 1
    p = products[0]
    # Not the bare brand "Logitech" — the longer full product title wins.
    assert p.title == "Logitech MX Master 3S Wireless Performance Mouse"
    assert p.url == "https://www.amazon.com/dp/B0BRANDED"  # canonicalized
