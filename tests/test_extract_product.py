"""Offline tests for the marketplace product extractor (no network)."""
from __future__ import annotations

import pytest

from ujin.extract.product import (
    SCRAPE_VERSION_CARD,
    SCRAPE_VERSION_DETAIL,
    Product,
    clean_product_name,
    extract_product_detail,
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


# ── Detail-page (rich) extraction ──────────────────────────────────────────────
# Compact stand-in for an Amazon product page: title, byline, rating popover, review
# count, buy-box price, a colorImages gallery var, an overview spec table, detail
# bullets (bidi-padded), and a selected twister variant. Mirrors the real DOM hooks
# verified against a live amazon.ca listing.
_DETAIL_HTML = """
<html><body>
<span id="productTitle">  SAMSUNG 990 PRO w/Heatsink SSD 2TB  </span>
<a id="bylineInfo" href="/stores/Samsung">Visit the Samsung Store</a>
<span id="acrPopover" title="4.5 out of 5 stars"></span>
<span id="acrCustomerReviewText">(4,994)</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$914.99</span></span>
</div>
<script type="text/javascript">
  var data = {'colorImages': {'initial': [
    {"hiRes":"https://m.media-amazon.com/images/I/one._SL1500_.jpg","thumb":"https://m.media-amazon.com/images/I/one._US40_.jpg","large":"https://m.media-amazon.com/images/I/one._SL500_.jpg","variant":"MAIN","main":{"https://x/one":[1500,1500]}},
    {"hiRes":null,"thumb":"https://m.media-amazon.com/images/I/two._US40_.jpg","large":"https://m.media-amazon.com/images/I/two._SL500_.jpg","variant":"PT01","main":{"https://x/two":[1500,1500]}}
  ]}};
</script>
<div id="productOverview_feature_div"><table>
  <tr><td><span class="a-text-bold">Digital storage capacity</span></td><td><span>2 TB</span></td></tr>
  <tr><td><span class="a-text-bold">Brand</span></td><td><span>Samsung</span></td></tr>
  <tr><td><span class="a-text-bold">Special feature</span></td>
      <td><script>window.junk = (function(){ return {a:1}; })();</script></td></tr>
</table></div>
<div id="detailBullets_feature_div"><ul>
  <li><span class="a-text-bold">Item model number ‏ : ‎</span> <span>MZ-V9P2T0CW</span></li>
</ul></div>
<span id="inline-twister-expanded-dimension-text-style_name"
      class="a-size-base inline-twister-dim-title-value a-text-bold"> 990 PRO HS </span>
<span class="a-size-base inline-twister-dim-title-value a-text-bold"> 2TB </span>
</body></html>
"""


def test_extract_product_detail_amazon():
    p = extract_product_detail(_DETAIL_HTML, "https://www.amazon.ca/dp/B0BHJDY57J/")
    assert isinstance(p, Product)
    assert p.title == "SAMSUNG 990 PRO w/Heatsink SSD 2TB"
    assert p.source_id == "B0BHJDY57J"
    assert p.price_cents == 91499
    assert p.currency == "CAD"               # inferred from the .ca storefront
    assert p.brand == "Samsung"
    assert p.rating == 4.5
    assert p.review_count == 4994
    assert p.variant == "990 PRO HS · 2TB"
    assert p.scrape_version == SCRAPE_VERSION_DETAIL
    # Gallery: hiRes preferred, falls back to large; ordered & deduped.
    assert p.images == [
        "https://m.media-amazon.com/images/I/one._SL1500_.jpg",
        "https://m.media-amazon.com/images/I/two._SL500_.jpg",
    ]
    assert p.image_url == p.images[0]
    # Specs: real facts kept; the script-debris value is dropped; bullet bidi marks stripped.
    assert p.specs["Digital storage capacity"] == "2 TB"
    assert p.specs["Item model number"] == "MZ-V9P2T0CW"
    assert "Special feature" not in p.specs


def test_extract_product_detail_requires_title_and_price():
    assert extract_product_detail("<html><body>nope</body></html>",
                                  "https://www.amazon.com/dp/B0X/") is None


def test_card_products_default_to_card_version():
    products = extract_products(_CARD_HTML, "https://www.amazon.com/s?k=power+bank")
    assert products[0].scrape_version == SCRAPE_VERSION_CARD


# ── AliExpress detail extraction (window.runParams JSON) ───────────────────────
# Compact stand-in for AliExpress's runParams blob (the fishing-rod listing shape):
# titleModule/priceModule/imageModule/specsModule/skuModule with a sale (activity) price.
_ALI_HTML = """
<html><body><script>
window.runParams = {"data": {
  "titleModule": {"subject": "3.6m Carbon Fiber Fishing Rod And Reel Combo",
                  "feedbackRating": {"averageStar": "4.9", "totalValidNum": 1059}},
  "priceModule": {
     "formatedActivityPrice": "C $30.58", "formatedPrice": "C $66.47",
     "minActivityAmount": {"value": 30.58, "currency": "CAD"},
     "minAmount": {"value": 66.47, "currency": "CAD"}},
  "imageModule": {"imagePathList": [
     "//ae01.alicdn.com/kf/one.jpg", "//ae01.alicdn.com/kf/two.jpg"]},
  "specsModule": {"props": [
     {"attrName": "Brand Name", "attrValue": "OEM"},
     {"attrName": "Material", "attrValue": "Carbon Fiber"},
     {"attrName": "Model Number", "attrValue": "XR-3600"}]},
  "skuModule": {"productSKUPropertyList": [
     {"skuPropertyName": "Color", "skuPropertyValues": [
        {"propertyValueDisplayName": "1.8M Rod 1000 Reel"},
        {"propertyValueDisplayName": "3.6M Rod 5000 Reel"}]}]}
}};
</script></body></html>
"""


def test_extract_product_detail_aliexpress():
    p = extract_product_detail(_ALI_HTML, "https://www.aliexpress.com/item/1005007170089155.html",
                               source="aliexpress")
    assert isinstance(p, Product)
    assert p.source == "aliexpress"
    assert p.source_id == "1005007170089155"
    assert p.title == "3.6m Carbon Fiber Fishing Rod And Reel Combo"
    assert p.price_cents == 3058           # activity (sale) price wins over the C$66.47 list price
    assert p.currency == "CAD"
    assert p.rating == 4.9
    assert p.review_count == 1059
    assert p.brand == "OEM"
    assert p.variant == "1.8M Rod 1000 Reel"
    assert p.scrape_version == SCRAPE_VERSION_DETAIL
    # Protocol-relative gallery URLs upgraded to https.
    assert p.images == ["https://ae01.alicdn.com/kf/one.jpg", "https://ae01.alicdn.com/kf/two.jpg"]
    assert p.image_url == p.images[0]
    assert p.specs["Material"] == "Carbon Fiber"


def test_aliexpress_bot_shell_returns_none():
    # The anti-bot shell has no runParams modules — must yield None, not a half-product.
    assert extract_product_detail("<html><body>JS required</body></html>",
                                  "https://www.aliexpress.com/item/123.html",
                                  source="aliexpress") is None


# ── Newegg-shaped card grid (tuple selectors, no id attr, link-derived id) ─────
# Mirrors the Newegg profile: tuple price/image selectors and a `.item-cell` card that
# carries NO data-id — the id must be recovered from the `/p/N82E...` product link.
# (Before the fix, passing a tuple to css_first raised a TypeError that silently zeroed
#  out the entire scrape — the cause of "0 Newegg rows" in production.)
_NEWEGG_CARD_HTML = """
<html><body>
<div class="item-cell">
  <a class="item-title" href="https://www.newegg.com/corsair-vengeance-ddr5/p/N82E16820982007">
     CORSAIR Vengeance 32GB (2 x 16GB) DDR5 6000 Desktop Memory</a>
  <div class="item-img"><img src="https://c1.neweggimages.com/p/20-236-828.jpg"/></div>
  <li class="price-current">$<strong>519</strong><sup>.99</sup>&nbsp;<abbr title="to">–</abbr></li>
</div>
</body></html>
"""

_NEWEGG_SELECTORS = {
    "card": ".item-cell", "id_attr": "data-id",
    "title": (".item-title",), "image": (".item-img img", "img"),
    "price": (".price-current", ".price-current strong"), "link": "a.item-title",
}


def test_newegg_cards_with_tuple_selectors():
    products = extract_products(
        _NEWEGG_CARD_HTML, "https://www.newegg.com/p/pl?d=ddr5+ram",
        source="newegg", selectors=_NEWEGG_SELECTORS,
    )
    assert len(products) == 1
    p = products[0]
    assert p.source == "newegg"
    assert p.source_id == "N82E16820982007"        # recovered from the /p/ link
    assert p.price_cents == 51999                   # ".price-current" -> "519.99"
    assert p.image_url.endswith("20-236-828.jpg")
    assert p.url.endswith("/p/N82E16820982007")


# ── Generic schema.org Product JSON-LD detail (Newegg, Shopify, …) ─────────────
_JSONLD_DETAIL_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"http://schema.org/","@type":"Product",
 "name":"CORSAIR Vengeance 32GB DDR5 6000 Desktop Memory",
 "description":"High-speed DDR5 gaming memory.",
 "sku":"N82E16820982007","mpn":"CMK32GX5M2B6000C30","brand":"Corsair",
 "image":"https://c1.neweggimages.com/p/20-236-828.jpg",
 "offers":{"@type":"Offer","price":"519.99","priceCurrency":"USD"},
 "aggregateRating":{"@type":"AggregateRating","ratingValue":4,"reviewCount":679},
 "weight":"0.2 oz"}
</script>
</head><body>
<div class="product-bullets"><li>DDR5 6000</li><li>CAS Latency 30</li></div>
</body></html>
"""


def test_extract_product_detail_jsonld_newegg():
    p = extract_product_detail(
        _JSONLD_DETAIL_HTML,
        "https://www.newegg.com/corsair/p/N82E16820982007",
        source="newegg",
    )
    assert isinstance(p, Product)
    assert p.source == "newegg"
    assert p.source_id == "N82E16820982007"
    assert p.title == "CORSAIR Vengeance 32GB DDR5 6000 Desktop Memory"
    assert p.price_cents == 51999
    assert p.currency == "USD"
    assert p.brand == "Corsair"
    assert p.rating == 4.0
    assert p.review_count == 679
    assert p.variant == "CMK32GX5M2B6000C30"
    assert p.image_url.endswith("20-236-828.jpg")
    assert p.scrape_version == SCRAPE_VERSION_DETAIL
    # Description prefers the on-page bullets (newegg selectors) over the JSON-LD blurb.
    assert "DDR5 6000" in p.description
    # Scalar schema props surface as specs; schema plumbing is skipped.
    assert p.specs.get("weight") == "0.2 oz"
    assert "offers" not in p.specs and "aggregateRating" not in p.specs


def test_amazon_detail_falls_back_to_jsonld_when_dom_absent():
    # No Amazon DOM (#productTitle etc.), but a valid Product JSON-LD block is present:
    # the Amazon path should fall back to JSON-LD rather than returning None.
    p = extract_product_detail(
        _JSONLD_DETAIL_HTML, "https://www.amazon.com/dp/B0ABC12345/", source="amazon",
    )
    assert isinstance(p, Product)
    assert p.price_cents == 51999
    assert p.scrape_version == SCRAPE_VERSION_DETAIL


def test_jsonld_detail_requires_price():
    no_price = _JSONLD_DETAIL_HTML.replace('"price":"519.99",', "")
    assert extract_product_detail(
        no_price, "https://www.newegg.com/x/p/N82E1", source="newegg") is None


# brand-as-object, image-as-list-of-ImageObject, no sku (id from link), gallery dedupe.
_JSONLD_RICH_HTML = """
<html><head>
<script type="application/ld+json">
{"@type":"Product",
 "name":"Generic Widget Pro",
 "brand":{"@type":"Brand","name":"Acme"},
 "image":[{"@type":"ImageObject","url":"https://cdn.example.com/a.jpg"},
          {"@type":"ImageObject","url":"https://cdn.example.com/a.jpg"},
          "https://cdn.example.com/b.jpg"],
 "offers":{"@type":"Offer","price":"12.50","priceCurrency":"EUR"},
 "color":"Blue"}
</script>
</head><body></body></html>
"""


def test_jsonld_detail_brand_object_and_image_list():
    p = extract_product_detail(
        _JSONLD_RICH_HTML, "https://shop.example.com/products/widget-pro", source="shopify",
    )
    assert p.brand == "Acme"                         # brand pulled out of the {name} object
    assert p.currency == "EUR"
    assert p.price_cents == 1250
    assert p.images == ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]  # deduped
    assert p.image_url == "https://cdn.example.com/a.jpg"
    assert p.specs.get("color") == "Blue"
    # No sku/mpn -> source_id recovered from the last path segment of the URL.
    assert p.source_id == "widget-pro"


def test_jsonld_detail_og_image_fallback():
    # Product block has no image; the extractor falls back to og:image.
    html = """
    <html><head>
    <meta property="og:image" content="https://cdn.example.com/og.jpg"/>
    <script type="application/ld+json">
    {"@type":"Product","name":"NoImg","offers":{"price":"9.99","priceCurrency":"USD"}}
    </script></head><body></body></html>
    """
    p = extract_product_detail(html, "https://shop.example.com/x", source="shopify")
    assert p.image_url == "https://cdn.example.com/og.jpg"


def test_jsonld_detail_no_product_block_returns_none():
    html = '<html><head><script type="application/ld+json">{"@type":"Article"}</script></head></html>'
    assert extract_product_detail(html, "https://x.com/a", source="newegg") is None


# ── eBay / Walmart card extraction + id recovery ───────────────────────────────
from ujin.extract.product import _id_from_href  # noqa: E402


def test_id_from_href_ebay():
    assert _id_from_href("https://www.ebay.com/itm/295678901234?hash=item", "ebay") == "295678901234"
    # Slug-style legacy URL: the numeric id is still recovered.
    assert _id_from_href("https://www.ebay.com/itm/Apple-iPhone/267890123456?epid=1", "ebay") == "267890123456"


def test_id_from_href_walmart():
    assert _id_from_href("https://www.walmart.com/ip/Great-Value-Coffee/12345678", "walmart") == "12345678"
    assert _id_from_href("https://www.walmart.com/ip/987654321", "walmart") == "987654321"


_EBAY_SELECTORS = {
    "card": ".s-card, .s-item",
    "id_attr": "data-id",
    "title": (".s-card__title", ".s-item__title span", ".s-item__title", "h3"),
    "image": (".s-card__image img", ".s-item__image-wrapper img", "img"),
    "price": (".s-card__price", ".s-item__price"),
    "link": ("a.su-link", "a.s-item__link", "a[href*='/itm/']"),
}

_EBAY_SRP_HTML = """
<html><body><ul class="srp-results">
<li class="s-item">
  <a class="s-item__link" href="https://www.ebay.com/itm/295678901234?hash=item">
    <div class="s-item__image-wrapper"><img src="https://i.ebayimg.com/images/g/abc/s-l300.jpg"/></div>
    <div class="s-item__title"><span role="heading">Apple AirPods Pro 2nd Generation Wireless Earbuds</span></div>
  </a>
  <span class="s-item__price">$179.99</span>
</li>
<li class="s-item">
  <a class="s-item__link" href="https://www.ebay.com/itm/305678901299">
    <div class="s-item__image-wrapper"><img src="https://i.ebayimg.com/images/g/xyz/s-l300.jpg"/></div>
    <div class="s-item__title"><span role="heading">Shop on eBay</span></div>
  </a>
  <span class="s-item__price">$0.99</span>
</li>
</ul></body></html>
"""


def test_extract_ebay_cards_and_skips_placeholder():
    products = extract_products(
        _EBAY_SRP_HTML, "https://www.ebay.com/sch/?_nkw=earbuds",
        source="ebay", selectors=_EBAY_SELECTORS,
    )
    # The "Shop on eBay" promo card is dropped; only the real listing remains.
    assert len(products) == 1
    p = products[0]
    assert p.source == "ebay"
    assert p.source_id == "295678901234"           # recovered from the /itm/ link
    assert p.title == "Apple AirPods Pro 2nd Generation Wireless Earbuds"
    assert p.price_cents == 17999
    assert p.image_url.endswith("s-l300.jpg")
    assert p.url.startswith("https://www.ebay.com/itm/295678901234")


_EBAY_BADGE_HTML = """
<html><body><ul class="srp-results">
<li class="s-item">
  <a class="s-item__link" href="https://www.ebay.com/itm/444555666777">
    <div class="s-item__image-wrapper"><img src="https://i.ebayimg.com/g/q.jpg"/></div>
    <div class="s-item__title"><span role="heading">New ListingSony WH-1000XM5 Headphones</span></div>
  </a>
  <span class="s-item__price">$299.00</span>
</li>
</ul></body></html>
"""


def test_ebay_title_strips_new_listing_badge():
    # eBay glues a "New Listing" badge to the front of the title span — strip it.
    products = extract_products(
        _EBAY_BADGE_HTML, "https://www.ebay.com/sch/?_nkw=headphones",
        source="ebay", selectors=_EBAY_SELECTORS,
    )
    assert len(products) == 1
    assert products[0].title == "Sony WH-1000XM5 Headphones"


_WALMART_SELECTORS = {
    "card": "[data-item-id]",
    "id_attr": "data-item-id",
    "title": ("[data-automation-id='product-title']", "span.w_iUH7", "a span"),
    "image": ("img[data-testid='productTileImage']", "img[loading]", "img"),
    "price": ("[data-automation-id='product-price'] .w_iUH7",
              "[data-automation-id='product-price']"),
    "link": ("a[link-identifier]", "a[href*='/ip/']"),
}

_WALMART_GRID_HTML = """
<html><body>
<div data-item-id="12345678">
  <a link-identifier="12345678" href="/ip/Great-Value-Classic-Roast-Coffee/12345678">
    <img data-testid="productTileImage" src="https://i5.walmartimages.com/asr/abc.jpg"/>
    <span data-automation-id="product-title">Great Value Classic Roast Ground Coffee, 30.5 oz</span>
  </a>
  <div data-automation-id="product-price"><span class="w_iUH7">current price $9.48</span></div>
</div>
</body></html>
"""


def test_extract_walmart_cards():
    products = extract_products(
        _WALMART_GRID_HTML, "https://www.walmart.com/search?q=coffee",
        source="walmart", selectors=_WALMART_SELECTORS,
    )
    assert len(products) == 1
    p = products[0]
    assert p.source == "walmart"
    assert p.source_id == "12345678"               # from the data-item-id card attr
    assert p.title.startswith("Great Value Classic Roast")
    assert p.price_cents == 948
    assert p.image_url.endswith("abc.jpg")
    assert p.url == "https://www.walmart.com/ip/Great-Value-Classic-Roast-Coffee/12345678"


def test_card_image_uses_data_src_when_src_placeholder():
    # A lazy-loaded grid: the <img> src is a 1px placeholder; the real URL is in data-src.
    html = """
    <html><body>
    <div data-component-type="s-search-result" data-asin="B0LAZY1234">
      <h2><a class="a-link-normal" href="/x"><span>Lazy Loaded Item</span></a></h2>
      <img class="s-image" src="data:image/gif;base64,AAAA" data-src="https://m.media-amazon.com/images/I/real.jpg"/>
      <span class="a-price"><span class="a-offscreen">$42.00</span></span>
    </div>
    </body></html>
    """
    products = extract_products(html, "https://www.amazon.com/s?k=x")
    assert len(products) == 1
    assert products[0].image_url == "https://m.media-amazon.com/images/I/real.jpg"
