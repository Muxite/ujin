"""Hermetic coverage gap-fill for ujin/extract/product.py.

Covers branches not exercised by test_extract_product.py:
  - extract_description: block/meta fallbacks, empty bullet text
  - _first_srcset, _card_image srcset path
  - _as_selectors with None / iterable
  - _id_from_href with None/empty href
  - clean_product_name("") early return
  - _currency_from_text with no symbol
  - _from_jsonld: non-dict block, wrong @type, bad offers, zero price, ImageObject, no title/image
  - _from_cards: card with no image
  - extract_product_detail Amazon: price selector iteration, no gallery, og:image fallback,
      no gallery + no og:image (None), URL without /dp/, no rating/review/byline
  - _detail_from_jsonld: non-dict block, no title, non-dict offers, no image, 14+ specs
  - _http_url_str: protocol-relative URL
  - _clean_float / _clean_pos_int: non-numeric / no-digit inputs
  - _balanced_json: escaped chars in strings, unclosed brace
  - _aliexpress_modules: marker-without-brace, unclosed JSON, invalid JSON,
      nested walk() shape
  - _ali_price_cents: structured-amount fallback chain, formatted-string fallback
  - AliExpress detail: no title, no price, no images, http:// gallery URL,
      bad averageStar (TypeError), review via totalValidNumStr, 14+ specs, variant dedupe
"""
from __future__ import annotations

import pytest

from ujin.extract.product import (
    SCRAPE_VERSION_DETAIL,
    Product,
    _ali_price_cents,
    _aliexpress_modules,
    _as_selectors,
    _balanced_json,
    _currency_from_text,
    _currency_from_url,
    _first_srcset,
    _id_from_href,
    clean_product_name,
    extract_description,
    extract_product_detail,
    extract_products,
    price_to_cents,
)


# ── clean_product_name ──────────────────────────────────────────────────────────

def test_clean_product_name_empty_string():
    # Falsy title returns immediately (line 305 in source).
    assert clean_product_name("") == ""


def test_clean_product_name_all_cut_leaves_nothing_fallback():
    # Title whose single word is stripped by the bracket regex → fallback to word-capped original.
    assert clean_product_name("(only brackets)") == "(only brackets)"


# ── price_to_cents ──────────────────────────────────────────────────────────────

def test_price_to_cents_int():
    assert price_to_cents(10) == 1000


def test_price_to_cents_float():
    assert price_to_cents(49.99) == 4999


def test_price_to_cents_no_digits():
    # Regex finds no match → None (line 284).
    assert price_to_cents("no price here") is None


# ── _as_selectors ──────────────────────────────────────────────────────────────

def test_as_selectors_none():
    assert _as_selectors(None) == ()


def test_as_selectors_string():
    assert _as_selectors(".foo") == (".foo",)


def test_as_selectors_tuple():
    assert _as_selectors((".a", ".b", "")) == (".a", ".b")


# ── _first_srcset ──────────────────────────────────────────────────────────────

def test_first_srcset_none():
    assert _first_srcset(None) is None


def test_first_srcset_empty():
    assert _first_srcset("") is None


def test_first_srcset_single():
    assert _first_srcset("https://cdn.example.com/img.jpg 1x") == "https://cdn.example.com/img.jpg"


def test_first_srcset_multiple():
    result = _first_srcset("https://cdn.example.com/a.jpg 1x, https://cdn.example.com/b.jpg 2x")
    assert result == "https://cdn.example.com/a.jpg"


# ── card image srcset path ──────────────────────────────────────────────────────

_SRCSET_CARD_HTML = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0SRCSET1">
  <h2><a class="a-link-normal" href="/dp/B0SRCSET1"><span>Srcset Widget</span></a></h2>
  <img class="s-image"
       srcset="https://m.media-amazon.com/images/I/srcset-a.jpg 1x, https://m.media-amazon.com/images/I/srcset-b.jpg 2x"/>
  <span class="a-price"><span class="a-offscreen">$29.99</span></span>
</div>
</body></html>
"""


def test_card_image_uses_srcset_when_no_src():
    products = extract_products(_SRCSET_CARD_HTML, "https://www.amazon.com/s?k=widget")
    assert len(products) == 1
    assert "srcset-a.jpg" in products[0].image_url


# ── _id_from_href ──────────────────────────────────────────────────────────────

def test_id_from_href_none():
    # line 252: returns None for falsy href.
    assert _id_from_href(None, "amazon") is None


def test_id_from_href_empty_string():
    assert _id_from_href("", "ebay") is None


def test_id_from_href_unknown_source_generic_fallback():
    # Source with no pattern → generic path tail.
    assert _id_from_href("https://example.com/products/my-widget", "unknown") == "my-widget"


def test_id_from_href_unknown_source_path_tail():
    # After stripping, generic fallback returns the last path segment.
    assert _id_from_href("https://example.com/products/", "unknown") == "products"


def test_id_from_href_wayfair():
    from ujin.extract.product import _id_from_href as f
    assert f("https://www.wayfair.com/pdp/cool-sofa-ABCD1234.html", "wayfair") == "ABCD1234"


def test_id_from_href_ikea():
    from ujin.extract.product import _id_from_href as f
    # IKEA pattern: `-(\d{8,})/` — digits must follow the dash directly.
    assert f("https://www.ikea.com/us/en/p/kallax-shelf-unit-09390333/", "ikea") == "09390333"


def test_id_from_href_google_shopping():
    from ujin.extract.product import _id_from_href as f
    assert f("https://www.google.com/shopping/product/12345678", "google_shopping") == "12345678"


# ── _currency_from_text / _currency_from_url ───────────────────────────────────

def test_currency_from_text_no_symbol_returns_default():
    # line 326: exhausts the loop, returns "USD".
    assert _currency_from_text("30.58") == "USD"


def test_currency_from_text_gbp():
    assert _currency_from_text("£12.50") == "GBP"


def test_currency_from_text_eur():
    assert _currency_from_text("€9.99") == "EUR"


def test_currency_from_text_custom_default():
    assert _currency_from_text("30.58", "CAD") == "CAD"


def test_currency_from_url_unknown_tld_falls_back_to_symbol():
    assert _currency_from_url("https://amazon.xyz/dp/B0", "£9.99") == "GBP"


def test_currency_from_url_no_amazon_in_url():
    assert _currency_from_url("https://shop.example.com/item/1", "$9.99") == "USD"


# ── extract_description ────────────────────────────────────────────────────────

_DESC_BLOCK_HTML = """
<html><head></head><body>
<div id="productDescription"><p>This widget is great for everyday use.</p></div>
</body></html>
"""

_DESC_META_HTML = """
<html>
<head>
  <meta name="description" content="Meta description for the product."/>
</head>
<body></body></html>
"""

_DESC_OG_HTML = """
<html>
<head>
  <meta property="og:description" content="OpenGraph product description."/>
</head>
<body></body></html>
"""

_DESC_EMPTY_BULLET_HTML = """
<html><body>
<div id="feature-bullets">
  <ul>
    <li><span class="a-list-item">   </span></li>
    <li><span class="a-list-item">Real bullet text</span></li>
  </ul>
</div>
</body></html>
"""


def test_extract_description_block_fallback():
    desc = extract_description(_DESC_BLOCK_HTML, source="amazon")
    assert desc is not None
    assert "great" in desc


def test_extract_description_meta_name_fallback():
    desc = extract_description(_DESC_META_HTML, source="amazon")
    assert desc == "Meta description for the product."


def test_extract_description_og_description_fallback():
    desc = extract_description(_DESC_OG_HTML, source="amazon")
    assert desc == "OpenGraph product description."


def test_extract_description_empty_bullet_skipped():
    # Bullet node with whitespace-only text is skipped; the real bullet is kept.
    desc = extract_description(_DESC_EMPTY_BULLET_HTML, source="amazon")
    assert "Real bullet text" in desc


def test_extract_description_no_content_returns_none():
    desc = extract_description("<html><body></body></html>", source="amazon")
    assert desc is None


def test_extract_description_custom_selectors():
    html = '<html><body><ul class="my-list"><li>Item one</li><li>Item two</li></ul></body></html>'
    desc = extract_description(html, selectors={"bullets": ".my-list li", "block": ".nonexistent"})
    assert desc is not None
    assert "Item one" in desc


def test_extract_description_max_chars_truncation():
    long_text = "word " * 200  # 1000 chars
    html = f'<html><head><meta name="description" content="{long_text}"/></head></html>'
    desc = extract_description(html, source="amazon", max_chars=50)
    assert desc is not None
    assert len(desc) <= 50


# ── _from_jsonld various skip branches ─────────────────────────────────────────

_JSONLD_NON_DICT_BLOCK = """
<html><head>
<script type="application/ld+json">
["not-a-dict", {"@type":"Product","name":"Widget","sku":"W1",
 "image":"https://img.example.com/w.jpg",
 "offers":{"price":"9.99","priceCurrency":"USD"}}]
</script>
</head><body></body></html>
"""

_JSONLD_WRONG_TYPE = """
<html><head>
<script type="application/ld+json">
[{"@type":"Article","name":"Blog post"},
 {"@type":"Product","name":"Widget2","sku":"W2",
  "image":"https://img.example.com/w2.jpg",
  "offers":{"price":"19.99","priceCurrency":"USD"}}]
</script>
</head><body></body></html>
"""

_JSONLD_NON_DICT_OFFERS = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Widget3","sku":"W3",
 "image":"https://img.example.com/w3.jpg",
 "offers":["offer1","offer2"]}
</script>
</head><body></body></html>
"""

_JSONLD_ZERO_PRICE = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Widget4","sku":"W4",
 "image":"https://img.example.com/w4.jpg",
 "offers":{"price":"0","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""

_JSONLD_IMAGE_OBJECT = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Widget5","sku":"W5",
 "image":{"@type":"ImageObject","url":"https://img.example.com/w5.jpg"},
 "offers":{"price":"29.99","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""

_JSONLD_NO_TITLE = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","sku":"W6",
 "image":"https://img.example.com/w6.jpg",
 "offers":{"price":"9.99","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""

_JSONLD_NO_IMAGE = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Widget7","sku":"W7",
 "offers":{"price":"9.99","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""


def test_from_jsonld_skips_non_dict_block():
    # A non-dict item in the JSON-LD list is skipped; the real Product block is returned.
    products = extract_products(_JSONLD_NON_DICT_BLOCK, "https://example.com/")
    assert len(products) == 1
    assert products[0].source_id == "W1"


def test_from_jsonld_skips_wrong_type():
    # @type != "Product" block is skipped.
    products = extract_products(_JSONLD_WRONG_TYPE, "https://example.com/")
    assert len(products) == 1
    assert products[0].source_id == "W2"


def test_from_jsonld_non_dict_offers_treated_as_empty():
    # A non-dict offers value means no extractable price → product skipped.
    products = extract_products(_JSONLD_NON_DICT_OFFERS, "https://example.com/")
    assert products == []


def test_from_jsonld_zero_price_skipped():
    products = extract_products(_JSONLD_ZERO_PRICE, "https://example.com/")
    assert products == []


def test_from_jsonld_image_as_imageobject():
    # image field is an ImageObject dict; the "url" key is extracted.
    products = extract_products(_JSONLD_IMAGE_OBJECT, "https://example.com/")
    assert len(products) == 1
    assert products[0].image_url == "https://img.example.com/w5.jpg"


def test_from_jsonld_no_title_skipped():
    products = extract_products(_JSONLD_NO_TITLE, "https://example.com/")
    assert products == []


def test_from_jsonld_no_image_skipped():
    products = extract_products(_JSONLD_NO_IMAGE, "https://example.com/")
    assert products == []


# ── card with no valid image ────────────────────────────────────────────────────

_NO_IMAGE_CARD_HTML = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0NOIMG11">
  <h2><a class="a-link-normal" href="/dp/B0NOIMG11"><span>Imageless Product</span></a></h2>
  <img class="s-image" src="data:image/gif;base64,AAAA"/>
  <span class="a-price"><span class="a-offscreen">$19.99</span></span>
</div>
</body></html>
"""


def test_card_with_no_valid_image_is_skipped():
    # Card has a data-URI src and no srcset/data-src → _card_image returns "" → skipped.
    products = extract_products(_NO_IMAGE_CARD_HTML, "https://www.amazon.com/s?k=x")
    assert products == []


# ── Amazon detail page — additional branches ────────────────────────────────────

_DETAIL_PRICE_SECOND_SELECTOR = """
<html><body>
<span id="productTitle">Widget Alpha</span>
<a id="bylineInfo">Brand: Acme</a>
<div id="corePrice_feature_div">
  <span class="a-price"><span class="a-offscreen">$49.99</span></span>
</div>
<script>
var data = {'colorImages': {'initial': [
  {"hiRes":"https://img.example.com/alpha.jpg","thumb":"https://t.example.com/t.jpg","large":"https://img.example.com/alpha-l.jpg"}
]}};
</script>
</body></html>
"""


def test_detail_price_found_at_second_selector():
    # First price selector (#corePriceDisplay_desktop...) absent; second (#corePrice...) hits.
    p = extract_product_detail(
        _DETAIL_PRICE_SECOND_SELECTOR,
        "https://www.amazon.com/dp/B0ALPHA111/",
    )
    assert p is not None
    assert p.price_cents == 4999
    assert p.brand == "Acme"


_DETAIL_TITLE_NO_PRICE_HTML = """
<html><body>
<span id="productTitle">Widget Beta - no price</span>
<script type="application/ld+json">
{"@type":"Product","name":"Widget Beta","sku":"BETA01",
 "image":"https://img.example.com/beta.jpg",
 "offers":{"price":"59.99","priceCurrency":"USD"}}
</script>
</body></html>
"""


def test_detail_title_present_price_missing_falls_back_to_jsonld():
    # DOM has a title but no buy-box price selector matches → jsonld fallback.
    p = extract_product_detail(
        _DETAIL_TITLE_NO_PRICE_HTML,
        "https://www.amazon.com/dp/B0BETA222/",
    )
    assert p is not None
    assert p.price_cents == 5999


_DETAIL_NO_RATING_NO_REVIEW_HTML = """
<html><body>
<span id="productTitle">Widget Gamma</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$39.99</span></span>
</div>
<script>
var data = {'colorImages': {'initial': [
  {"hiRes":"https://img.example.com/gamma.jpg","thumb":"https://t.example.com/t.jpg","large":"https://img.example.com/gl.jpg"}
]}};
</script>
</body></html>
"""


def test_detail_no_rating_no_review_no_byline():
    # No #acrPopover, no #acrCustomerReviewText, no #bylineInfo → all None.
    p = extract_product_detail(
        _DETAIL_NO_RATING_NO_REVIEW_HTML,
        "https://www.amazon.com/dp/B0GAMMA33/",
    )
    assert p is not None
    assert p.rating is None
    assert p.review_count is None
    assert p.brand is None


_DETAIL_RATING_NO_MATCH_HTML = """
<html><body>
<span id="productTitle">Widget Delta</span>
<span id="acrPopover" title="no star info here"></span>
<span id="acrCustomerReviewText">plenty of reviews</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$19.99</span></span>
</div>
<script>
var data = {'colorImages': {'initial': [
  {"hiRes":"https://img.example.com/delta.jpg","thumb":"https://t.example.com/t.jpg","large":"https://img.example.com/dl.jpg"}
]}};
</script>
</body></html>
"""


def test_detail_rating_title_attr_no_match():
    # #acrPopover exists but title attr does not match _RATING_RE → rating None.
    # #acrCustomerReviewText exists but text has no digits → review_count None.
    p = extract_product_detail(
        _DETAIL_RATING_NO_MATCH_HTML,
        "https://www.amazon.com/dp/B0DELTA44/",
    )
    assert p is not None
    assert p.rating is None
    assert p.review_count is None


_DETAIL_OG_IMAGE_FALLBACK_HTML = """
<html><head>
<meta property="og:image" content="https://img.example.com/og.jpg"/>
</head><body>
<span id="productTitle">Widget Epsilon</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$9.99</span></span>
</div>
</body></html>
"""


def test_detail_no_gallery_falls_back_to_og_image():
    # No colorImages gallery script → falls back to og:image meta.
    p = extract_product_detail(
        _DETAIL_OG_IMAGE_FALLBACK_HTML,
        "https://www.amazon.com/dp/B0EPSILON/",
    )
    assert p is not None
    assert p.image_url == "https://img.example.com/og.jpg"


_DETAIL_NO_GALLERY_NO_OG_HTML = """
<html><body>
<span id="productTitle">Widget Zeta</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$9.99</span></span>
</div>
</body></html>
"""


def test_detail_no_gallery_no_og_image_returns_none():
    # No colorImages, no og:image → returns None (line 767).
    p = extract_product_detail(
        _DETAIL_NO_GALLERY_NO_OG_HTML,
        "https://www.amazon.com/dp/B0ZETAAA1/",
    )
    assert p is None


_DETAIL_URL_NO_DP_HTML = """
<html><body>
<span id="productTitle">Widget Eta</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$12.34</span></span>
</div>
<script>
var data = {'colorImages': {'initial': [
  {"hiRes":"https://img.example.com/eta.jpg","thumb":"https://t.example.com/t.jpg","large":"https://img.example.com/etal.jpg"}
]}};
</script>
</body></html>
"""


def test_detail_url_without_dp_source_id_is_none():
    # URL has no /dp/ or /gp/product/ → source_id stays None.
    p = extract_product_detail(
        _DETAIL_URL_NO_DP_HTML,
        "https://www.amazon.com/Widget-Eta/",
        source="amazon",
    )
    assert p is not None
    assert p.source_id is None


def test_detail_amazon_with_custom_selectors():
    # Passing explicit selectors merges with defaults; non-amazon source uses jsonld path.
    p = extract_product_detail(
        _DETAIL_NO_RATING_NO_REVIEW_HTML,
        "https://www.amazon.com/dp/B0GAMMA55/",
        selectors={},  # empty overrides → same as no overrides
    )
    assert p is not None
    assert p.price_cents == 3999


# ── _detail_from_jsonld branches ───────────────────────────────────────────────

_DETAIL_JSONLD_NON_DICT_BLOCK = """
<html><head>
<script type="application/ld+json">
["string-item", {"@type":"Product","name":"Kappa Widget",
 "image":"https://img.example.com/kappa.jpg",
 "offers":{"price":"7.77","priceCurrency":"USD"}}]
</script>
</head><body></body></html>
"""


def test_detail_jsonld_skips_non_dict_in_block_list():
    p = extract_product_detail(
        _DETAIL_JSONLD_NON_DICT_BLOCK,
        "https://shop.example.com/kappa",
        source="shopify",
    )
    assert p is not None
    assert p.price_cents == 777


_DETAIL_JSONLD_NO_NAME = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","image":"https://img.example.com/x.jpg",
 "offers":{"price":"1.00","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""


def test_detail_jsonld_no_name_returns_none():
    # Line 580: return None when title is empty.
    p = extract_product_detail(
        _DETAIL_JSONLD_NO_NAME,
        "https://shop.example.com/x",
        source="shopify",
    )
    assert p is None


_DETAIL_JSONLD_LIST_OFFERS = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Lambda Widget",
 "image":"https://img.example.com/lam.jpg",
 "offers":[{"price":"5.00"},{"price":"6.00"}]}
</script>
</head><body></body></html>
"""


def test_detail_jsonld_list_offers_first_taken():
    # offers is a list → _first() picks the first element → price extracted.
    p = extract_product_detail(
        _DETAIL_JSONLD_LIST_OFFERS,
        "https://shop.example.com/lam",
        source="shopify",
    )
    assert p is not None
    assert p.price_cents == 500


_DETAIL_JSONLD_NO_IMAGE_NO_OG = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Mu Widget",
 "offers":{"price":"3.00","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""


def test_detail_jsonld_no_image_no_og_returns_none():
    # No image in block, no og:image → returns None (line 605).
    p = extract_product_detail(
        _DETAIL_JSONLD_NO_IMAGE_NO_OG,
        "https://shop.example.com/mu",
        source="shopify",
    )
    assert p is None


_DETAIL_JSONLD_PROTOCOL_RELATIVE_IMAGE = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Nu Widget",
 "image":"//img.example.com/nu.jpg",
 "offers":{"price":"11.11","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""


def test_detail_jsonld_protocol_relative_image_upgraded():
    # Protocol-relative URL "//..." is converted to "https://..." (line 661).
    p = extract_product_detail(
        _DETAIL_JSONLD_PROTOCOL_RELATIVE_IMAGE,
        "https://shop.example.com/nu",
        source="shopify",
    )
    assert p is not None
    assert p.image_url == "https://img.example.com/nu.jpg"


_DETAIL_JSONLD_BAD_RATING = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Xi Widget",
 "image":"https://img.example.com/xi.jpg",
 "offers":{"price":"5.00","priceCurrency":"USD"},
 "aggregateRating":{"ratingValue":"not-a-number","reviewCount":"N/A"}}
</script>
</head><body></body></html>
"""


def test_detail_jsonld_bad_rating_and_review_count():
    # _clean_float with non-numeric → None (line 670-671).
    # _clean_pos_int with "N/A" (no digits) → None (line 677).
    p = extract_product_detail(
        _DETAIL_JSONLD_BAD_RATING,
        "https://shop.example.com/xi",
        source="shopify",
    )
    assert p is not None
    assert p.rating is None
    assert p.review_count is None


_DETAIL_JSONLD_MANY_SPECS = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Omicron Widget","sku":"OMI1",
 "image":"https://img.example.com/omi.jpg",
 "offers":{"price":"99.99","priceCurrency":"USD"},
 "attr1":"v1","attr2":"v2","attr3":"v3","attr4":"v4","attr5":"v5",
 "attr6":"v6","attr7":"v7","attr8":"v8","attr9":"v9","attr10":"v10",
 "attr11":"v11","attr12":"v12","attr13":"v13","attr14":"v14","attr15":"v15"}
</script>
</head><body></body></html>
"""


def test_detail_jsonld_specs_capped_at_14():
    # More than 14 scalar attrs → the specs dict is capped at 14 (line 630->632).
    p = extract_product_detail(
        _DETAIL_JSONLD_MANY_SPECS,
        "https://shop.example.com/omi",
        source="shopify",
    )
    assert p is not None
    assert len(p.specs) <= 14


_DETAIL_JSONLD_NO_CURRENCY = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Pi Widget","sku":"PI1",
 "image":"https://img.example.com/pi.jpg",
 "offers":{"price":"4.99"}}
</script>
</head><body></body></html>
"""


def test_detail_jsonld_currency_inferred_from_url():
    # No priceCurrency in offers → inferred from amazon TLD.
    p = extract_product_detail(
        _DETAIL_JSONLD_NO_CURRENCY,
        "https://www.amazon.co.uk/dp/B0PI111/",
        source="amazon",
    )
    assert p is not None
    assert p.currency == "GBP"


# ── _balanced_json ──────────────────────────────────────────────────────────────

def test_balanced_json_simple():
    text = '{"key": "value"}'
    assert _balanced_json(text, 0) == text


def test_balanced_json_with_escaped_quote():
    # Backslash inside a string flips esc; the inner `"` is not a closer (lines 808, 810).
    text = '{"k": "val\\"ue"}'
    result = _balanced_json(text, 0)
    assert result == text


def test_balanced_json_unclosed_brace_returns_none():
    # EOF before depth reaches 0 → None (line 821).
    text = '{"key": "value"'
    assert _balanced_json(text, 0) is None


def test_balanced_json_nested():
    text = '{"outer": {"inner": 1}}'
    assert _balanced_json(text, 0) == text


def test_balanced_json_with_offset():
    text = 'prefix {"key": 1}'
    assert _balanced_json(text, 7) == '{"key": 1}'


# ── _aliexpress_modules error paths ────────────────────────────────────────────

def test_aliexpress_modules_returns_none_for_no_marker():
    # No runParams at all → None.
    assert _aliexpress_modules("<html><body>nothing here</body></html>") is None


def test_aliexpress_modules_marker_without_brace():
    # Marker found but no '{' follows it → continue (line 832).
    html = "<script>window.runParams = no-json-here; more = {};</script>"
    result = _aliexpress_modules(html)
    # The second `= {}` is a valid (but empty) object with no modules → None.
    assert result is None


def test_aliexpress_modules_unclosed_brace_returns_none():
    # _balanced_json returns None for unclosed braces → continue (line 835).
    html = "<script>window.runParams = { unclosed"
    assert _aliexpress_modules(html) is None


def test_aliexpress_modules_invalid_json_continues():
    # _balanced_json finds balanced braces but content is not valid JSON (line 838-839).
    html = "<script>window.runParams = { invalid json }</script>"
    assert _aliexpress_modules(html) is None


def test_aliexpress_modules_nested_walk_shape():
    # Modules nested inside a non-"data" key — the walk() function finds them.
    import json
    data = {
        "pageComponent": {
            "titleModule": {"subject": "Nested Widget"},
            "priceModule": {"formatedPrice": "$5.00"},
        }
    }
    html = f"<script>window.runParams = {json.dumps(data)};</script>"
    result = _aliexpress_modules(html)
    assert result is not None
    assert "titleModule" in result


# ── _ali_price_cents ───────────────────────────────────────────────────────────

def test_ali_price_cents_structured_activity_price():
    # minActivityAmount has a value dict → first match.
    cents, currency = _ali_price_cents({"minActivityAmount": {"value": 30.58, "currency": "CAD"}})
    assert cents == 3058
    assert currency == "CAD"


def test_ali_price_cents_skip_none_value_falls_to_formatted():
    # minActivityAmount value is None → skipped; minAmount value also None →
    # falls through to formatedPrice string (lines 867->865, 872-876).
    price_mod = {
        "minActivityAmount": {"value": None, "currency": "USD"},
        "formatedActivityPrice": "US $7.50",
    }
    cents, currency = _ali_price_cents(price_mod)
    assert cents == 750


def test_ali_price_cents_non_dict_amount_falls_to_formatted():
    # Amount is not a dict → skipped.
    price_mod = {
        "minActivityAmount": "not-a-dict",
        "formatedPrice": "€12.99",
    }
    cents, currency = _ali_price_cents(price_mod)
    assert cents == 1299
    assert currency == "EUR"


def test_ali_price_cents_no_structured_no_formatted():
    # Empty price module → (None, "USD").
    cents, currency = _ali_price_cents({})
    assert cents is None
    assert currency == "USD"


def test_ali_price_cents_max_amount_fallback():
    # minActivityAmount and minAmount are absent; maxActivityAmount hits (line 869->865 loop).
    price_mod = {
        "maxActivityAmount": {"value": 99.99, "currency": "USD"},
    }
    cents, currency = _ali_price_cents(price_mod)
    assert cents == 9999
    assert currency == "USD"


# ── AliExpress detail edge cases ───────────────────────────────────────────────

def _ali_html(data: dict) -> str:
    import json
    return f"<html><body><script>window.runParams = {json.dumps({'data': data})};</script></body></html>"


def test_aliexpress_detail_no_title_returns_none():
    data = {
        "titleModule": {},
        "priceModule": {"minActivityAmount": {"value": 10.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/img.jpg"]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/123.html",
                               source="aliexpress")
    assert p is None


def test_aliexpress_detail_no_price_returns_none():
    data = {
        "titleModule": {"subject": "Widget"},
        "priceModule": {},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/img.jpg"]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/456.html",
                               source="aliexpress")
    assert p is None


def test_aliexpress_detail_no_images_returns_none():
    data = {
        "titleModule": {"subject": "Widget"},
        "priceModule": {"minActivityAmount": {"value": 10.0, "currency": "USD"}},
        "imageModule": {"imagePathList": []},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/789.html",
                               source="aliexpress")
    assert p is None


def test_aliexpress_detail_http_gallery_url():
    # Gallery URL already starts with https:// (not protocol-relative) → used as-is.
    data = {
        "titleModule": {"subject": "Widget"},
        "priceModule": {"minActivityAmount": {"value": 5.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/kf/direct.jpg"]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/999.html",
                               source="aliexpress")
    assert p is not None
    assert p.image_url == "https://ae01.alicdn.com/kf/direct.jpg"


def test_aliexpress_detail_bad_average_star():
    # averageStar is non-numeric → TypeError caught → rating is None (lines 912-913).
    data = {
        "titleModule": {
            "subject": "Widget",
            "feedbackRating": {"averageStar": "not-a-float", "totalValidNum": 100},
        },
        "priceModule": {"minActivityAmount": {"value": 5.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/kf/x.jpg"]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/111.html",
                               source="aliexpress")
    assert p is not None
    assert p.rating is None
    assert p.review_count == 100


def test_aliexpress_detail_review_via_totalValidNumStr():
    # totalValidNum absent, totalValidNumStr present (covers loop iteration branch 916->914).
    data = {
        "titleModule": {
            "subject": "Widget",
            "feedbackRating": {"totalValidNumStr": "2,345"},
        },
        "priceModule": {"minActivityAmount": {"value": 5.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/kf/y.jpg"]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/222.html",
                               source="aliexpress")
    assert p is not None
    assert p.review_count == 2345


def test_aliexpress_detail_review_only_via_star_rate_key():
    # None of the primary review keys have digits → review_count stays None.
    data = {
        "titleModule": {
            "subject": "Widget",
            "feedbackRating": {"evarageStarRate": "4.5"},
        },
        "priceModule": {"minActivityAmount": {"value": 5.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/kf/z.jpg"]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/333.html",
                               source="aliexpress")
    assert p is not None
    assert p.review_count is None


def test_aliexpress_detail_specs_at_limit():
    # 15 spec props → only the first 14 are kept (line 928->930 break at limit).
    props = [{"attrName": f"key{i}", "attrValue": f"val{i}"} for i in range(15)]
    data = {
        "titleModule": {"subject": "Widget"},
        "priceModule": {"minActivityAmount": {"value": 5.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/kf/s.jpg"]},
        "specsModule": {"props": props},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/444.html",
                               source="aliexpress")
    assert p is not None
    assert len(p.specs) == 14


def test_aliexpress_detail_url_without_item_id():
    # URL does not match /item/<digits>.html → source_id is None.
    data = {
        "titleModule": {"subject": "Widget"},
        "priceModule": {"minActivityAmount": {"value": 5.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/kf/t.jpg"]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/store/widget",
                               source="aliexpress")
    assert p is not None
    assert p.source_id is None


def test_aliexpress_detail_variant_dedupe():
    # Multiple SKU properties populate the variant field.
    data = {
        "titleModule": {"subject": "Widget"},
        "priceModule": {"minActivityAmount": {"value": 5.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/kf/u.jpg"]},
        "skuModule": {"productSKUPropertyList": [
            {"skuPropertyValues": [{"propertyValueDisplayName": "Red"}]},
            {"skuPropertyValues": [{"propertyValueDisplayName": "Large"}]},
        ]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/555.html",
                               source="aliexpress")
    assert p is not None
    assert p.variant == "Red · Large"


def test_aliexpress_detail_brand_from_specs():
    # Brand is extracted from a spec with attrName == "brand".
    data = {
        "titleModule": {"subject": "Widget"},
        "priceModule": {"minActivityAmount": {"value": 5.0, "currency": "USD"}},
        "imageModule": {"imagePathList": ["https://ae01.alicdn.com/kf/v.jpg"]},
        "specsModule": {"props": [
            {"attrName": "brand", "attrValue": "Acme"},
            {"attrName": "Material", "attrValue": "Steel"},
        ]},
    }
    p = extract_product_detail(_ali_html(data), "https://www.aliexpress.com/item/666.html",
                               source="aliexpress")
    assert p is not None
    assert p.brand == "Acme"
    assert p.specs.get("Material") == "Steel"
