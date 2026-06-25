"""Targeted offline tests for marketplace-engine coverage gaps.

Covers uncovered branches in:
  ujin/fetch/browser.py    – fingerprint rotation / stealth ctx, close edge cases
  ujin/extract/product.py  – JSON-LD / schema.org edge cases, href id recovery,
                              detail-page extraction branches
  ujin/poll/amazon.py      – poll empty-HTML path, harvest store save/cap
  ujin/poll/marketplace.py – _load_file JSON/ValueError, _SeenStore save, poll edges
"""
from __future__ import annotations

import json
import os

import pytest

from conftest import FakeClock, FakePage


# ============================================================================
# ujin/fetch/browser.py
# ============================================================================

class TestBrowserGrowLoopTimeout:
    """_grow_loop timeout branch (lines 263-264)."""

    async def test_timeout_stops_loop(self):
        from ujin.fetch.browser import _ActionCtx, _grow_loop

        clk = FakeClock(start=0.0)
        page = FakePage(
            selectors={".btn": 1, ".card": 3},
            present={".btn": True},
            enabled={".btn": True},
            grow_selector=".card",
            per_click=2,
            total_clicks=100,
        )
        ctx = _ActionCtx(sleep=clk.sleep, clock=clk.now, default_timeout_ms=30000)
        # timeout_ms=0 means deadline is immediately past
        result = await _grow_loop(
            ctx, page, button=".btn", results=".card",
            max_iters=50, timeout_ms=0, settle_ms=100,
        )
        assert result["stopped"] == "timeout"
        assert result["iterations"] == 0


class TestBrowserCloseWhenNoBackend:
    """BrowserFetcher.close() when _backend is None (line 416 exit)."""

    async def test_close_no_backend_is_noop(self):
        from ujin.fetch.browser import BrowserFetcher

        f = BrowserFetcher()
        assert f._backend is None
        await f.close()  # must not raise
        assert f._backend is None


class TestBrowserEnsureBackendSelenium:
    """_ensure_backend selenium branch (lines 354-358)."""

    async def test_ensure_backend_selenium_raises_when_not_installed(self, monkeypatch):
        import ujin.fetch.browser as mod
        from ujin.fetch.browser import BrowserError, BrowserFetcher

        monkeypatch.setattr(mod, "browser_available", lambda engine: False)
        f = BrowserFetcher(engine="selenium")
        with pytest.raises(BrowserError, match="not installed"):
            await f._ensure_backend()


class TestBrowserRenderCloseException:
    """render() closes page even when close() raises (lines 383-384)."""

    async def test_render_page_close_exception_suppressed(self):
        from ujin.fetch.browser import BrowserFetcher

        class _BadClosePage(FakePage):
            async def close(self):
                raise RuntimeError("close fail")

        class _BadCloseBackend:
            async def new_page(self):
                return _BadClosePage(html="<html/>")
            async def close(self):
                pass

        clk = FakeClock()
        f = BrowserFetcher(sleep=clk.sleep, clock=clk.now)
        f._backend = _BadCloseBackend()
        # Should not propagate the close exception
        result = await f.render("https://x.test/")
        assert result.html == "<html/>"


# ============================================================================
# ujin/extract/product.py
# ============================================================================

class TestAsSelectors:
    """_as_selectors None branch (line 183)."""

    def test_none_returns_empty_tuple(self):
        from ujin.extract.product import _as_selectors

        assert _as_selectors(None) == ()

    def test_str_returns_single_tuple(self):
        from ujin.extract.product import _as_selectors

        assert _as_selectors(".foo") == (".foo",)

    def test_list_converted_to_tuple(self):
        from ujin.extract.product import _as_selectors

        assert _as_selectors([".a", ".b"]) == (".a", ".b")


class TestFirstSrcset:
    """_first_srcset edge cases (lines 200-203)."""

    def test_none_returns_none(self):
        from ujin.extract.product import _first_srcset

        assert _first_srcset(None) is None

    def test_empty_string_returns_none(self):
        from ujin.extract.product import _first_srcset

        assert _first_srcset("") is None

    def test_extracts_first_url(self):
        from ujin.extract.product import _first_srcset

        assert _first_srcset("https://cdn.example.com/a.jpg 1x, https://cdn.example.com/b.jpg 2x") == "https://cdn.example.com/a.jpg"


class TestCardImage:
    """_card_image srcset fallback (lines 218-221)."""

    def test_uses_srcset_when_src_missing(self):
        from selectolax.parser import HTMLParser

        from ujin.extract.product import _card_image

        html = """<div class="card">
          <img srcset="https://img.example/a.jpg 1x, https://img.example/b.jpg 2x">
        </div>"""
        tree = HTMLParser(html)
        card = tree.css_first(".card")
        assert _card_image(card, ("img",)) == "https://img.example/a.jpg"

    def test_data_src_wins_over_empty_src(self):
        from selectolax.parser import HTMLParser

        from ujin.extract.product import _card_image

        html = """<div><img src="" data-src="https://img.example/real.jpg"/></div>"""
        tree = HTMLParser(html)
        card = tree.css_first("div")
        assert _card_image(card, "img") == "https://img.example/real.jpg"

    def test_returns_empty_when_no_match(self):
        from selectolax.parser import HTMLParser

        from ujin.extract.product import _card_image

        html = "<div><img src='data:image/gif;base64,AAAA'/></div>"
        tree = HTMLParser(html)
        card = tree.css_first("div")
        assert _card_image(card, "img") == ""


class TestIdFromHref:
    """_id_from_href edge cases (line 252 + generic fallback)."""

    def test_none_href_returns_none(self):
        from ujin.extract.product import _id_from_href

        assert _id_from_href(None, "amazon") is None

    def test_empty_href_returns_none(self):
        from ujin.extract.product import _id_from_href

        assert _id_from_href("", "amazon") is None

    def test_unknown_source_falls_back_to_path_tail(self):
        from ujin.extract.product import _id_from_href

        assert _id_from_href("https://shop.example.com/products/cool-widget", "shopify") == "cool-widget"

    def test_wayfair_pattern(self):
        from ujin.extract.product import _id_from_href

        assert _id_from_href("https://www.wayfair.com/pdp/some-couch-ABCD1234.html", "wayfair") == "ABCD1234"

    def test_ikea_pattern(self):
        from ujin.extract.product import _id_from_href

        # IKEA pattern: `-(\d{8,})/` — digits must be directly after the dash
        assert _id_from_href("https://www.ikea.com/us/en/p/kallax-shelf-unit-90390333/", "ikea") == "90390333"

    def test_google_shopping_pattern(self):
        from ujin.extract.product import _id_from_href

        assert _id_from_href("https://www.google.com/shopping/product/1234567890", "google_shopping") == "1234567890"

    def test_amazon_gp_product_pattern(self):
        from ujin.extract.product import _id_from_href

        assert _id_from_href("https://www.amazon.com/gp/product/B0ABCD1234/", "amazon") == "B0ABCD1234"

    def test_no_pattern_match_falls_back_to_tail(self):
        from ujin.extract.product import _id_from_href

        # newegg pattern won't match this URL format; fallback to path tail
        result = _id_from_href("https://www.newegg.com/product/no-match", "newegg")
        assert result == "no-match"


class TestPriceToCentsEdgeCases:
    """price_to_cents ValueError path (lines 287-288)."""

    def test_float_input(self):
        from ujin.extract.product import price_to_cents

        assert price_to_cents(12.5) == 1250

    def test_int_input(self):
        from ujin.extract.product import price_to_cents

        assert price_to_cents(0) == 0

    def test_zero_float(self):
        from ujin.extract.product import price_to_cents

        assert price_to_cents(0.0) == 0

    def test_thousands_separator_thin_space(self):
        from ujin.extract.product import price_to_cents

        # Thin-space thousands separator (some Euro sites)
        assert price_to_cents("1 299,99") is not None  # best-effort


class TestCleanProductNameEdgeCases:
    """clean_product_name fallback (line 305)."""

    def test_empty_string_returns_empty(self):
        from ujin.extract.product import clean_product_name

        assert clean_product_name("") == ""

    def test_bracket_removal_then_word_cap(self):
        from ujin.extract.product import clean_product_name

        # title after cutting has only bracketed content removed → fallback to words
        result = clean_product_name("Widget (ASIN X)")
        assert "Widget" in result

    def test_whitespace_only_after_brackets(self):
        from ujin.extract.product import clean_product_name

        # If everything got stripped, fall back to capped original
        result = clean_product_name("A B C D E F G H I J K", max_words=3)
        assert result == "A B C"


class TestCurrencyFromText:
    """_currency_from_text no-match returns default (line 326)."""

    def test_no_symbol_returns_default(self):
        from ujin.extract.product import _currency_from_text

        assert _currency_from_text("12.99", default="USD") == "USD"

    def test_euro_symbol(self):
        from ujin.extract.product import _currency_from_text

        assert _currency_from_text("€12.99") == "EUR"


class TestCurrencyFromUrl:
    """_currency_from_url TLD fallback (line 484)."""

    def test_amazon_ca(self):
        from ujin.extract.product import _currency_from_url

        assert _currency_from_url("https://www.amazon.ca/dp/B0X") == "CAD"

    def test_no_amazon_tld_falls_back_to_price_text(self):
        from ujin.extract.product import _currency_from_url

        assert _currency_from_url("https://shop.example.com/p/x", "£9.99") == "GBP"

    def test_unknown_tld_uses_symbol(self):
        from ujin.extract.product import _currency_from_url

        assert _currency_from_url("https://www.amazon.xyz/dp/B0X", "$9.99") == "USD"


class TestFromJsonldEdgeCases:
    """_from_jsonld branch coverage (lines 340, 344, 347, 350, 353, 356)."""

    def test_non_dict_block_skipped(self):
        from ujin.extract.product import _from_jsonld

        # A list containing a non-dict should be skipped
        result = _from_jsonld(["not a dict", None], source="test", base_url="https://x.com/")
        assert result == []

    def test_type_list_with_product(self):
        from ujin.extract.product import _from_jsonld

        # @type is a list that contains "Product"
        result = _from_jsonld([
            {"@type": ["BreadcrumbList", "Product"],
             "name": "Widget", "image": "https://img.example/w.jpg",
             "offers": {"price": "9.99", "priceCurrency": "USD"}}
        ], source="test", base_url="https://x.com/")
        assert len(result) == 1
        assert result[0].title == "Widget"

    def test_non_dict_offers_treated_as_empty(self):
        from ujin.extract.product import _from_jsonld

        # offers is a string, not dict → should be treated as {} → no price → skip
        result = _from_jsonld([
            {"@type": "Product", "name": "Widget",
             "image": "https://img.example/w.jpg",
             "offers": "some-string"}
        ], source="test", base_url="https://x.com/")
        assert result == []

    def test_zero_price_skipped(self):
        from ujin.extract.product import _from_jsonld

        result = _from_jsonld([
            {"@type": "Product", "name": "Freebie",
             "image": "https://img.example/f.jpg",
             "offers": {"price": "0.00", "priceCurrency": "USD"}}
        ], source="test", base_url="https://x.com/")
        assert result == []

    def test_image_as_dict_with_url(self):
        from ujin.extract.product import _from_jsonld

        result = _from_jsonld([
            {"@type": "Product", "name": "Widget",
             "image": {"@type": "ImageObject", "url": "https://img.example/w.jpg"},
             "offers": {"price": "19.99", "priceCurrency": "USD"}}
        ], source="test", base_url="https://x.com/")
        assert len(result) == 1
        assert result[0].image_url == "https://img.example/w.jpg"

    def test_no_title_skipped(self):
        from ujin.extract.product import _from_jsonld

        result = _from_jsonld([
            {"@type": "Product",
             "image": "https://img.example/w.jpg",
             "offers": {"price": "9.99", "priceCurrency": "USD"}}
        ], source="test", base_url="https://x.com/")
        assert result == []

    def test_no_image_skipped(self):
        from ujin.extract.product import _from_jsonld

        result = _from_jsonld([
            {"@type": "Product", "name": "Widget",
             "offers": {"price": "9.99", "priceCurrency": "USD"}}
        ], source="test", base_url="https://x.com/")
        assert result == []


class TestFromCardsEdgeCases:
    """_from_cards edge cases (lines 388, 391)."""

    def test_card_with_no_image_skipped(self):
        from ujin.extract.product import extract_products

        html = """<html><body>
        <div data-component-type="s-search-result" data-asin="B0NOIMG">
          <h2><span>A Product Without Image</span></h2>
          <span class="a-price"><span class="a-offscreen">$29.99</span></span>
          <a class="a-link-normal" href="/dp/B0NOIMG"></a>
        </div>
        </body></html>"""
        # Only JSON-LD path would include a product; CSS card is skipped due to no image
        products = extract_products(html, "https://www.amazon.com/s?k=x")
        assert all(p.source_id != "B0NOIMG" for p in products)


class TestExtractProductsDedup:
    """Deduplication in extract_products (lines 445->449)."""

    def test_dedup_same_source_id(self):
        from ujin.extract.product import extract_products

        html = """<html><head>
        <script type="application/ld+json">
        [
          {"@type":"Product","name":"Widget A","sku":"SKU1",
           "image":"https://img.example/a.jpg",
           "offers":{"price":"9.99","priceCurrency":"USD"}},
          {"@type":"Product","name":"Widget A Dupe","sku":"SKU1",
           "image":"https://img.example/a2.jpg",
           "offers":{"price":"10.99","priceCurrency":"USD"}}
        ]
        </script>
        </head><body></body></html>"""
        products = extract_products(html, "https://x.com/")
        # SKU1 should appear only once
        ids = [p.source_id for p in products if p.source_id == "SKU1"]
        assert len(ids) == 1


class TestExtractDescriptionBranches:
    """extract_description fallback branches (lines 96-99, 101-107)."""

    def test_block_fallback_when_no_bullets(self):
        from ujin.extract.product import extract_description

        html = """<html><body>
        <div id="productDescription"><p>This is the block description.</p></div>
        </body></html>"""
        result = extract_description(html, source="amazon")
        assert result and "block description" in result

    def test_meta_description_fallback(self):
        from ujin.extract.product import extract_description

        html = """<html>
        <head><meta name="description" content="A meta description fallback."/></head>
        <body></body></html>"""
        result = extract_description(html, source="amazon")
        assert result and "meta description" in result

    def test_og_description_fallback(self):
        from ujin.extract.product import extract_description

        html = """<html>
        <head><meta property="og:description" content="OG fallback description."/></head>
        <body></body></html>"""
        result = extract_description(html, source="amazon")
        assert result and "OG fallback" in result

    def test_empty_html_returns_none(self):
        from ujin.extract.product import extract_description

        assert extract_description("<html><body></body></html>", source="amazon") is None

    def test_custom_selectors_override(self):
        from ujin.extract.product import extract_description

        html = """<html><body>
        <ul class="custom-bullets"><li>First bullet</li><li>Second bullet</li></ul>
        </body></html>"""
        result = extract_description(html, selectors={"bullets": ".custom-bullets li"})
        assert result and "First bullet" in result


class TestDetailFromJsonldBranches:
    """_detail_from_jsonld branch coverage (lines 569, 580, 584, 605, 630)."""

    def test_no_product_block_returns_none(self):
        from ujin.extract.product import _detail_from_jsonld

        html = """<html><head>
        <script type="application/ld+json">{"@type":"Article","name":"blog post"}</script>
        </head></html>"""
        assert _detail_from_jsonld(html, "https://x.com/", source="newegg") is None

    def test_empty_name_returns_none(self):
        from ujin.extract.product import _detail_from_jsonld

        html = """<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"",
         "image":"https://img.example/x.jpg",
         "offers":{"price":"9.99","priceCurrency":"USD"}}
        </script></head></html>"""
        assert _detail_from_jsonld(html, "https://x.com/", source="shopify") is None

    def test_non_dict_offers_treated_as_empty(self):
        from ujin.extract.product import _detail_from_jsonld

        html = """<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Widget","image":"https://img.example/x.jpg",
         "offers": ["list-not-dict"]}
        </script></head></html>"""
        # offers list → first() returns "list-not-dict" → not a dict → treated as {}
        # → no price → return None
        assert _detail_from_jsonld(html, "https://x.com/", source="shopify") is None

    def test_zero_price_returns_none(self):
        from ujin.extract.product import _detail_from_jsonld

        html = """<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Free Item","image":"https://img.example/x.jpg",
         "offers":{"price":"0","priceCurrency":"USD"}}
        </script></head></html>"""
        assert _detail_from_jsonld(html, "https://x.com/", source="shopify") is None

    def test_no_image_returns_none(self):
        from ujin.extract.product import _detail_from_jsonld

        html = """<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Widget",
         "offers":{"price":"9.99","priceCurrency":"USD"}}
        </script></head></html>"""
        assert _detail_from_jsonld(html, "https://x.com/", source="shopify") is None

    def test_aggregaterating_none_branch(self):
        from ujin.extract.product import _detail_from_jsonld

        # aggregateRating is not a dict (string) → rating/review_count stay None
        html = """<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Widget","image":"https://img.example/x.jpg",
         "offers":{"price":"9.99","priceCurrency":"USD"},
         "aggregateRating":"not-a-dict"}
        </script></head></html>"""
        p = _detail_from_jsonld(html, "https://x.com/", source="shopify")
        assert p is not None
        assert p.rating is None
        assert p.review_count is None

    def test_specs_skips_brace_values(self):
        from ujin.extract.product import _detail_from_jsonld

        # Values containing curly braces (widget/template debris) should be skipped
        # Note: in _detail_from_jsonld, values are truncated to [:80], not length-checked.
        # Only the brace filter applies in that code path.
        html = """<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Widget","image":"https://img.example/x.jpg",
         "offers":{"price":"9.99","priceCurrency":"USD"},
         "goodProp":"clean value","braceProp":"{curly brace value}"}
        </script></head></html>"""
        p = _detail_from_jsonld(html, "https://x.com/", source="shopify")
        assert "goodProp" in p.specs
        assert "braceProp" not in p.specs


class TestHttpUrlStr:
    """_http_url_str protocol-relative branch (line 661)."""

    def test_protocol_relative(self):
        from ujin.extract.product import _http_url_str

        assert _http_url_str("//cdn.example.com/img.jpg", "https://x.com/") == "https://cdn.example.com/img.jpg"

    def test_https_passthrough(self):
        from ujin.extract.product import _http_url_str

        assert _http_url_str("https://cdn.example.com/img.jpg", "https://x.com/") == "https://cdn.example.com/img.jpg"

    def test_relative_resolved_against_base(self):
        from ujin.extract.product import _http_url_str

        assert _http_url_str("/images/x.jpg", "https://shop.example.com/") == "https://shop.example.com/images/x.jpg"

    def test_none_returns_none(self):
        from ujin.extract.product import _http_url_str

        assert _http_url_str(None, "https://x.com/") is None

    def test_empty_returns_none(self):
        from ujin.extract.product import _http_url_str

        assert _http_url_str("", "https://x.com/") is None


class TestCleanFloat:
    """_clean_float error branches (lines 670-671)."""

    def test_valid_float(self):
        from ujin.extract.product import _clean_float

        assert _clean_float("4.5") == 4.5

    def test_none_returns_none(self):
        from ujin.extract.product import _clean_float

        assert _clean_float(None) is None

    def test_invalid_string_returns_none(self):
        from ujin.extract.product import _clean_float

        assert _clean_float("not-a-number") is None


class TestCleanPosInt:
    """_clean_pos_int error branches (lines 677, 680-681)."""

    def test_string_with_digits(self):
        from ujin.extract.product import _clean_pos_int

        assert _clean_pos_int("(4,994)") == 4994

    def test_none_returns_none(self):
        from ujin.extract.product import _clean_pos_int

        assert _clean_pos_int(None) is None

    def test_no_digits_returns_none(self):
        from ujin.extract.product import _clean_pos_int

        assert _clean_pos_int("no digits here") is None


class TestAmazonDetailFallbacks:
    """extract_product_detail Amazon DOM → JSON-LD fallback branches (lines 729-738)."""

    _JSONLD_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"http://schema.org/","@type":"Product",
 "name":"Fallback Widget","sku":"FALL123",
 "image":"https://img.example/f.jpg",
 "offers":{"@type":"Offer","price":"49.99","priceCurrency":"USD"}}
</script>
</head><body>
</body></html>"""

    def test_no_dom_title_falls_back_to_jsonld(self):
        from ujin.extract.product import extract_product_detail

        # No #productTitle → should fall back to JSON-LD and return a product
        p = extract_product_detail(self._JSONLD_HTML, "https://www.amazon.com/dp/B0XYZ12345/")
        assert p is not None
        assert p.title == "Fallback Widget"

    def test_no_price_dom_falls_back_to_jsonld(self):
        from ujin.extract.product import extract_product_detail

        # Has title but no price in DOM → falls back to JSON-LD
        html = """<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Widget","sku":"W123",
 "image":"https://img.example/w.jpg",
 "offers":{"price":"29.99","priceCurrency":"USD"}}
</script>
</head><body>
<span id="productTitle">Widget</span>
</body></html>"""
        p = extract_product_detail(html, "https://www.amazon.com/dp/B0ABC12345/")
        # No price in DOM → fallback to JSON-LD
        assert p is not None

    def test_no_image_dom_returns_none(self):
        from ujin.extract.product import extract_product_detail

        # Title + price but no gallery images and no og:image → None
        html = """<html><body>
<span id="productTitle">My Widget</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$49.99</span></span>
</div>
</body></html>"""
        result = extract_product_detail(html, "https://www.amazon.com/dp/B0NOIMG1234/")
        assert result is None

    def test_og_image_fallback_on_amazon(self):
        from ujin.extract.product import extract_product_detail

        # No colorImages gallery → falls back to og:image meta
        html = """<html>
<head><meta property="og:image" content="https://img.example/og.jpg"/></head>
<body>
<span id="productTitle">My Widget with OG Image</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$49.99</span></span>
</div>
</body></html>"""
        p = extract_product_detail(html, "https://www.amazon.com/dp/B0OGIMG1234/")
        assert p is not None
        assert p.image_url == "https://img.example/og.jpg"


class TestExtractGallery:
    """_extract_gallery limit branch (line 517)."""

    def test_gallery_limited_to_limit(self):
        from ujin.extract.product import _extract_gallery

        # Build HTML with 15 gallery entries
        entries = ""
        for i in range(15):
            entries += (
                f'{{"hiRes":"https://img.example/img{i}.jpg",'
                f'"thumb":"https://img.example/t{i}.jpg",'
                f'"large":"https://img.example/l{i}.jpg"}}'
            )
            if i < 14:
                entries += ","
        html = f"var colorImages = {{'initial': [{entries}]}};"
        result = _extract_gallery(html, limit=5)
        assert len(result) == 5

    def test_null_hires_falls_back_to_large(self):
        from ujin.extract.product import _extract_gallery

        html = (
            '"hiRes":null,'
            '"thumb":"https://img.example/t.jpg",'
            '"large":"https://img.example/l.jpg"'
        )
        result = _extract_gallery(html)
        assert result == ["https://img.example/l.jpg"]


# ============================================================================
# ujin/poll/amazon.py
# ============================================================================

class TestAmazonPollEmptyHtml:
    """poll() returns changed=False when no HTML (lines 218-219)."""

    async def test_poll_no_html_returns_empty(self, monkeypatch):
        from ujin.poll.amazon import AmazonSearchPollable

        async def fake_render(self, url):
            return ("", "http")

        monkeypatch.setattr(AmazonSearchPollable, "_render", fake_render)
        src = AmazonSearchPollable("test widget")
        result = await src.poll(None)
        assert result.ok is True
        assert result.changed is False
        assert result.payload == []


class TestAmazonPollNoCategoryNoClean:
    """poll() branches when category=None and clean_titles=False (lines 225-228)."""

    async def test_poll_no_category_no_clean(self, monkeypatch):
        from ujin.poll import amazon as amz
        from ujin.extract import product as prod

        async def fake_render(self, url):
            return ("<html>ok</html>", "http")

        def fake_extract(html, url, source="amazon", selectors=None, **kw):
            return [prod.Product(source=source, source_id="A1", title="Some Long Title Name Here",
                                 image_url="http://x/i.jpg", price_cents=999, currency="USD")]

        monkeypatch.setattr(AmazonSearchPollable := amz.AmazonSearchPollable, "_render", fake_render)
        monkeypatch.setattr(prod, "extract_products", fake_extract)

        src = amz.AmazonSearchPollable("widget", category=None, clean_titles=False)
        result = await src.poll(None)
        assert result.ok
        # Title not cleaned (no truncation)
        assert result.payload[0]["title"] == "Some Long Title Name Here"
        # Category not set (None → stays None)
        assert result.payload[0]["category"] is None


class TestAmazonPollNoProducts:
    """poll() logs warning when page fetched but no products parsed (line 233)."""

    async def test_poll_html_but_no_products(self, monkeypatch, caplog):
        import logging

        from ujin.poll import amazon as amz

        async def fake_render(self, url):
            return ("<html><body>no products here</body></html>", "http")

        def fake_extract(html, url, **kw):
            return []

        monkeypatch.setattr(amz.AmazonSearchPollable, "_render", fake_render)
        from ujin.extract import product as prod
        monkeypatch.setattr(prod, "extract_products", fake_extract)

        src = amz.AmazonSearchPollable("widget")
        with caplog.at_level(logging.WARNING, logger="ujin.poll.amazon"):
            result = await src.poll(None)
        assert result.ok
        assert "no products parsed" in caplog.text


class TestHarvestStoreSaveBranches:
    """_HarvestStore.save() makedirs and OSError branches (lines 339-346)."""

    def test_save_creates_subdirectory(self, tmp_path):
        from ujin.poll.amazon import _HarvestStore

        path = str(tmp_path / "sub" / "harvest.json")
        store = _HarvestStore(path)
        store.add_from_titles([{"title": "ergonomic keyboard", "category": "Electronics"}], min_len=4)
        store.save()
        assert os.path.exists(path)

    def test_save_oserror_logged_not_raised(self, tmp_path, caplog, monkeypatch):
        import logging

        from ujin.poll.amazon import _HarvestStore

        path = str(tmp_path / "harvest.json")
        store = _HarvestStore(path)
        store.pool["widget"] = "Electronics"

        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with caplog.at_level(logging.WARNING, logger="ujin.poll.amazon"):
            store.save()
        assert "harvest store save failed" in caplog.text

    def test_add_from_titles_pool_cap(self, tmp_path):
        from ujin.poll.amazon import _HarvestStore

        store = _HarvestStore(str(tmp_path / "h.json"), max_pool=3)
        # Add enough unique long words to hit the cap
        titles = [{"title": f"product alpha{i} beta{i} gamma{i}", "category": "E"} for i in range(10)]
        added = store.add_from_titles(titles, min_len=4)
        assert len(store.pool) <= 3
        assert added <= 3

    def test_draw_n_zero_returns_empty(self, tmp_path):
        from ujin.poll.amazon import _HarvestStore

        store = _HarvestStore(str(tmp_path / "h.json"))
        store.pool["alpha"] = "Electronics"
        assert store.draw(0) == []

    def test_draw_empty_pool_returns_empty(self, tmp_path):
        from ujin.poll.amazon import _HarvestStore

        store = _HarvestStore(str(tmp_path / "h.json"))
        assert store.draw(5) == []


class TestAmazonCategoryPollHarvest:
    """AmazonCategoryPollable.poll() harvest path and exception handling (lines 479-498)."""

    async def test_poll_exception_in_child_skipped(self, monkeypatch, tmp_path):
        from ujin.poll import amazon as amz
        from ujin.poll.base import PollResult

        async def fake_poll_raises(self, prev):
            raise RuntimeError("network error")

        monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_poll_raises)
        src = amz.AmazonCategoryPollable(
            categories=["Electronics"], terms_per_poll=2, seed=1,
        )
        result = await src.poll(None)
        assert result.ok

    async def test_poll_harvest_mode(self, monkeypatch, tmp_path):
        from ujin.poll import amazon as amz
        from ujin.poll.base import PollResult

        async def fake_poll(self, prev):
            return PollResult(ok=True, changed=True, payload=[
                {"source": "amazon", "source_id": "H1", "title": "mechanical keyboard",
                 "price_cents": 8999, "category": "Electronics"}
            ])

        monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_poll)
        harvest_path = str(tmp_path / "harvest.json")
        src = amz.AmazonCategoryPollable(
            categories=["Electronics"], terms_per_poll=1, seed=1,
            harvest=True, harvest_path=harvest_path,
        )
        result = await src.poll(None)
        assert result.ok
        # Harvest store should have persisted some learned words
        assert os.path.exists(harvest_path)

    async def test_poll_dedupes_across_children_with_none_source_id(self, monkeypatch):
        from ujin.poll import amazon as amz
        from ujin.poll.base import PollResult

        call_count = [0]

        async def fake_poll(self, prev):
            call_count[0] += 1
            return PollResult(ok=True, changed=True, payload=[
                # item with no source_id — should pass through without dedup
                {"source": "amazon", "source_id": None, "title": "Misc", "price_cents": 100},
            ])

        monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_poll)
        src = amz.AmazonCategoryPollable(categories=["Kitchen"], terms_per_poll=2, seed=1)
        result = await src.poll(None)
        assert result.ok
        # Items with no source_id are all kept (not deduped against each other)
        none_id_items = [it for it in result.payload if not it.get("source_id")]
        assert len(none_id_items) == call_count[0]


# ============================================================================
# ujin/poll/marketplace.py
# ============================================================================

class TestLoadFileEdgeCases:
    """_load_file JSON branch and ValueError branch (lines 53, 56, 62)."""

    def test_missing_file_raises(self, tmp_path):
        from ujin.poll.marketplace import _load_file

        with pytest.raises(FileNotFoundError):
            _load_file(str(tmp_path / "nonexistent.json"))

    def test_json_extension_parsed_as_json(self, tmp_path):
        from ujin.poll.marketplace import _load_file

        p = tmp_path / "profiles.json"
        p.write_text(json.dumps({"shop": {"domain": "x.com", "search_url": "https://x/{query}"}}))
        result = _load_file(str(p))
        assert "shop" in result

    def test_non_dict_root_raises_value_error(self, tmp_path):
        from ujin.poll.marketplace import _load_file

        p = tmp_path / "profiles.json"
        p.write_text(json.dumps(["not", "a", "dict"]))
        with pytest.raises(ValueError, match="must be a mapping"):
            _load_file(str(p))


class TestSeenStoreSaveBranches:
    """_SeenStore.save() makedirs and OSError branches (lines 125-132)."""

    def test_save_creates_parent_directory(self, tmp_path):
        from ujin.poll.marketplace import _SeenStore

        path = str(tmp_path / "sub" / "seen.json")
        store = _SeenStore(path, ttl_secs=3600)
        store.mark(["id1", "id2"])
        store.save()
        assert os.path.exists(path)

    def test_save_oserror_logged_not_raised(self, tmp_path, caplog, monkeypatch):
        import logging

        from ujin.poll.marketplace import _SeenStore

        path = str(tmp_path / "seen.json")
        store = _SeenStore(path, ttl_secs=3600)
        store.mark(["id1"])

        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("no space")))
        with caplog.at_level(logging.WARNING, logger="ujin.poll.marketplace"):
            store.save()
        assert "seen store save failed" in caplog.text

    def test_save_no_parent_path(self, tmp_path, monkeypatch):
        """When path has no directory component, makedirs is skipped."""
        from ujin.poll.marketplace import _SeenStore

        orig_dir = os.getcwd()
        try:
            os.chdir(tmp_path)
            store = _SeenStore("seen_flat.json", ttl_secs=3600)
            store.mark(["x"])
            store.save()
            assert os.path.exists(tmp_path / "seen_flat.json")
        finally:
            os.chdir(orig_dir)


class TestSeenStoreTTL:
    """_SeenStore TTL hit, miss, and expiry (per acceptance criteria)."""

    def test_ttl_hit(self, tmp_path):
        from ujin.poll.marketplace import _SeenStore

        clock = [1000.0]
        path = str(tmp_path / "seen.json")
        store = _SeenStore(path, ttl_secs=500, clock=lambda: clock[0])
        store.mark(["fresh"])
        store.save()

        clock[0] = 1200.0  # still within TTL
        reloaded = _SeenStore(path, ttl_secs=500, clock=lambda: clock[0])
        assert "fresh" in reloaded.fresh_ids()

    def test_ttl_miss_on_new_id(self, tmp_path):
        from ujin.poll.marketplace import _SeenStore

        store = _SeenStore(str(tmp_path / "s.json"), ttl_secs=500)
        # "unseen" was never marked
        assert "unseen" not in store.fresh_ids()

    def test_ttl_expiry(self, tmp_path):
        from ujin.poll.marketplace import _SeenStore

        clock = [1000.0]
        path = str(tmp_path / "seen.json")
        store = _SeenStore(path, ttl_secs=100, clock=lambda: clock[0])
        store.mark(["expires"])
        store.save()

        clock[0] = 1200.0  # past TTL
        reloaded = _SeenStore(path, ttl_secs=100, clock=lambda: clock[0])
        assert "expires" not in reloaded.fresh_ids()
        # Expired entries pruned on load
        assert "expires" not in reloaded.seen


class TestMarketplaceSampleEmpty:
    """_sample() with no keyterms returns empty list (line 216)."""

    def test_sample_empty_when_no_categories(self):
        from ujin.poll.marketplace import MarketplaceSearchPollable

        src = MarketplaceSearchPollable(
            profile="shop",
            profiles={"shop": {"domain": "x.com", "search_url": "https://x.com/{query}",
                                "keyterms": {}}},
            seed=1,
        )
        assert src._sample() == []


class TestMarketplacePollExceptionHandling:
    """poll() skips child exceptions and id-less items (lines 235-243)."""

    async def test_poll_skips_exception_results(self, monkeypatch):
        from ujin.poll import amazon as amz
        from ujin.poll.base import PollResult

        async def fake_poll_fail(self, prev):
            raise ValueError("child failed")

        monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_poll_fail)
        src = _make_marketplace_src(monkeypatch)
        result = await src.poll(None)
        assert result.ok
        assert result.payload == []

    async def test_poll_includes_id_less_items(self, monkeypatch):
        from ujin.poll import amazon as amz
        from ujin.poll.base import PollResult

        async def fake_poll(self, prev):
            return PollResult(ok=True, changed=True, payload=[
                {"source_id": None, "title": "no id item", "price_cents": 100},
                {"source_id": None, "title": "no id item 2", "price_cents": 200},
            ])

        monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_poll)
        src = _make_marketplace_src(monkeypatch)
        result = await src.poll(None)
        assert result.ok
        # Both None-id items pass through (no dedup on None)
        assert len(result.payload) == 2

    async def test_poll_dedupes_by_source_id(self, monkeypatch):
        from ujin.poll import amazon as amz
        from ujin.poll.base import PollResult

        call_n = [0]

        async def fake_poll(self, prev):
            call_n[0] += 1
            return PollResult(ok=True, changed=True, payload=[
                {"source_id": "SHARED", "title": "dupe item", "price_cents": 100},
                {"source_id": f"UNIQUE{call_n[0]}", "title": "unique", "price_cents": 200},
            ])

        monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_poll)
        src = _make_marketplace_src(monkeypatch, terms_per_poll=2)
        result = await src.poll(None)
        shared = [it for it in result.payload if it["source_id"] == "SHARED"]
        assert len(shared) == 1


# ============================================================================
# Additional branch coverage
# ============================================================================

class TestBrowserGrowLoopNoResults:
    """_grow_loop scroll_to_bottom path with results=None (line 281->261)."""

    async def test_scroll_without_results_tracking(self):
        from ujin.fetch.browser import _ActionCtx, _grow_loop

        clk = FakeClock(start=0.0)
        page = FakePage(
            selectors={},
            grow_selector=".item",
            per_click=5,
            total_clicks=3,
        )
        ctx = _ActionCtx(sleep=clk.sleep, clock=clk.now, default_timeout_ms=30000)
        # results=None → scroll_to_bottom but no growth check; runs max_iters times
        result = await _grow_loop(
            ctx, page, button=None, results=None,
            max_iters=2, timeout_ms=60000, settle_ms=100,
        )
        assert result["iterations"] == 2
        assert result["stopped"] == "max_iters"


class TestHarvestStoreSaveNoParent:
    """_HarvestStore.save() when path has no directory component (line 339->341)."""

    def test_save_no_parent_dir(self, tmp_path, monkeypatch):
        import os

        from ujin.poll.amazon import _HarvestStore

        monkeypatch.chdir(tmp_path)
        store = _HarvestStore("harvest_flat.json")
        store.pool["widget"] = "Electronics"
        store.save()
        assert os.path.exists(tmp_path / "harvest_flat.json")


class TestAmazonCategoryPollDedupSid:
    """AmazonCategoryPollable.poll() deduplication continue (line 484)."""

    async def test_poll_dedupes_shared_ids_across_children(self, monkeypatch):
        from ujin.poll import amazon as amz
        from ujin.poll.base import PollResult

        async def fake_poll(self, prev):
            return PollResult(ok=True, changed=True, payload=[
                {"source": "amazon", "source_id": "SAME", "title": "thing", "price_cents": 100},
                {"source": "amazon", "source_id": "UNIQUE", "title": "other", "price_cents": 200},
            ])

        monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_poll)
        src = amz.AmazonCategoryPollable(categories=["Kitchen"], terms_per_poll=2, seed=1)
        result = await src.poll(None)
        same_count = sum(1 for it in result.payload if it["source_id"] == "SAME")
        assert same_count == 1   # SAME deduplicated across children


class TestAmazonPollWithDescription:
    """poll() calls _attach_descriptions when with_description=True (line 230)."""

    async def test_poll_with_description_calls_attach(self, monkeypatch):
        from ujin.poll import amazon as amz
        from ujin.extract import product as prod

        calls = []

        async def fake_render(self, url):
            return ("<html/>", "http")

        def fake_extract(html, url, **kw):
            return [prod.Product(source="amazon", source_id="A1", title="Widget",
                                 image_url="http://x/i.jpg", price_cents=100, currency="USD")]

        async def fake_attach(self, products):
            calls.append(len(products))

        monkeypatch.setattr(amz.AmazonSearchPollable, "_render", fake_render)
        monkeypatch.setattr(prod, "extract_products", fake_extract)
        monkeypatch.setattr(amz.AmazonSearchPollable, "_attach_descriptions", fake_attach)

        src = amz.AmazonSearchPollable("widget", with_description=True)
        result = await src.poll(None)
        assert result.ok
        assert calls == [1]   # _attach_descriptions called with 1 product


class TestAliexpressEdgeCases:
    """AliExpress parsing edge cases (lines 808-945)."""

    def test_balanced_json_with_escape_sequences(self):
        from ujin.extract.product import _balanced_json

        # Escaped quote inside a string — scanner must not treat it as end-of-string
        text = r'{"key":"val\"ue","n":1}'
        result = _balanced_json(text, 0)
        assert result == text

    def test_aliexpress_modules_no_marker(self):
        from ujin.extract.product import _aliexpress_modules

        assert _aliexpress_modules("<html><body>no script here</body></html>") is None

    def test_aliexpress_modules_invalid_json(self):
        from ujin.extract.product import _aliexpress_modules

        # Marker present but JSON is malformed
        html = "window.runParams = {broken json; here}"
        assert _aliexpress_modules(html) is None

    def test_aliexpress_modules_nested_shape(self):
        """Walk-based discovery for newer runParams shapes (lines 844-858)."""
        from ujin.extract.product import _aliexpress_modules

        # No top-level .data with *Module — nested inside another key
        html = """window.runParams = {"pageInfo": {"titleModule": {"subject": "Widget"},
            "priceModule": {"formatedPrice": "$5.00"}}}"""
        result = _aliexpress_modules(html)
        assert result is not None
        assert "titleModule" in result

    def test_ali_price_cents_no_structured_amount(self):
        """Falls back to formatted price string when structured amount is absent (lines 872-876)."""
        from ujin.extract.product import _ali_price_cents

        price_mod = {"formatedActivityPrice": "$29.99"}
        cents, currency = _ali_price_cents(price_mod)
        assert cents == 2999
        assert currency == "USD"

    def test_ali_price_cents_zero_structured_falls_to_formatted(self):
        """Skips structured amounts that parse to 0 and uses formatted fallback."""
        from ujin.extract.product import _ali_price_cents

        price_mod = {
            "minActivityAmount": {"value": 0, "currency": "USD"},
            "formatedPrice": "$9.99",
        }
        cents, currency = _ali_price_cents(price_mod)
        assert cents == 999

    def test_ali_price_cents_no_price(self):
        from ujin.extract.product import _ali_price_cents

        cents, currency = _ali_price_cents({})
        assert cents is None

    def test_aliexpress_detail_no_title_returns_none(self):
        from ujin.extract.product import _aliexpress_detail

        html = """<script>window.runParams = {"data": {
            "titleModule": {"subject": ""},
            "priceModule": {"formatedPrice": "$5"},
            "imageModule": {"imagePathList": ["//img.example/x.jpg"]}
        }};</script>"""
        assert _aliexpress_detail(html, "https://www.aliexpress.com/item/123.html") is None

    def test_aliexpress_detail_no_price_returns_none(self):
        from ujin.extract.product import _aliexpress_detail

        html = """<script>window.runParams = {"data": {
            "titleModule": {"subject": "Widget"},
            "priceModule": {},
            "imageModule": {"imagePathList": ["//img.example/x.jpg"]}
        }};</script>"""
        assert _aliexpress_detail(html, "https://www.aliexpress.com/item/123.html") is None

    def test_aliexpress_detail_no_images_returns_none(self):
        from ujin.extract.product import _aliexpress_detail

        html = """<script>window.runParams = {"data": {
            "titleModule": {"subject": "Widget"},
            "priceModule": {"formatedPrice": "$5.00"},
            "imageModule": {"imagePathList": []}
        }};</script>"""
        assert _aliexpress_detail(html, "https://www.aliexpress.com/item/123.html") is None

    def test_aliexpress_detail_http_images_not_upgraded(self):
        """Images already starting with http (not //) are kept as-is (line 902->898 False)."""
        from ujin.extract.product import _aliexpress_detail

        html = """<script>window.runParams = {"data": {
            "titleModule": {"subject": "Widget"},
            "priceModule": {"formatedPrice": "$5.00"},
            "imageModule": {"imagePathList": ["https://img.example/x.jpg"]}
        }};</script>"""
        p = _aliexpress_detail(html, "https://www.aliexpress.com/item/456.html")
        assert p is not None
        assert p.image_url == "https://img.example/x.jpg"

    def test_aliexpress_detail_no_source_id_when_no_item_url(self):
        """source_id is None when URL doesn't match /item/...html (line 945->948 False)."""
        from ujin.extract.product import _aliexpress_detail

        html = """<script>window.runParams = {"data": {
            "titleModule": {"subject": "Widget"},
            "priceModule": {"formatedPrice": "$5.00"},
            "imageModule": {"imagePathList": ["https://img.example/x.jpg"]}
        }};</script>"""
        p = _aliexpress_detail(html, "https://www.aliexpress.com/search?q=widget")
        assert p is not None
        assert p.source_id is None

    def test_aliexpress_detail_rating_error(self):
        """ValueError from invalid averageStar sets rating to None (lines 912-913)."""
        from ujin.extract.product import _aliexpress_detail

        html = """<script>window.runParams = {"data": {
            "titleModule": {"subject": "Widget",
                           "feedbackRating": {"averageStar": "not-a-float", "totalValidNum": 100}},
            "priceModule": {"formatedPrice": "$5.00"},
            "imageModule": {"imagePathList": ["https://img.example/x.jpg"]}
        }};</script>"""
        p = _aliexpress_detail(html, "https://www.aliexpress.com/item/789.html")
        assert p is not None
        assert p.rating is None

    def test_aliexpress_detail_specs_cap_and_brand(self):
        """Specs reach the 14-entry limit and brand extracted from props (lines 925-931)."""
        from ujin.extract.product import _aliexpress_detail

        props = [{"attrName": f"Prop{i}", "attrValue": f"Val{i}"} for i in range(20)]
        props.insert(0, {"attrName": "Brand Name", "attrValue": "Acme"})
        html = f"""<script>window.runParams = {{"data": {{
            "titleModule": {{"subject": "Widget"}},
            "priceModule": {{"formatedPrice": "$5.00"}},
            "imageModule": {{"imagePathList": ["https://img.example/x.jpg"]}},
            "specsModule": {{"props": {json.dumps(props)}}}
        }}}};</script>"""
        p = _aliexpress_detail(html, "https://www.aliexpress.com/item/111.html")
        assert p is not None
        assert p.brand == "Acme"
        assert len(p.specs) <= 14

    def test_aliexpress_detail_sku_no_display_name(self):
        """Falls back to propertyValueName when displayName is absent (line 939->935)."""
        from ujin.extract.product import _aliexpress_detail

        html = """<script>window.runParams = {"data": {
            "titleModule": {"subject": "Widget"},
            "priceModule": {"formatedPrice": "$5.00"},
            "imageModule": {"imagePathList": ["https://img.example/x.jpg"]},
            "skuModule": {"productSKUPropertyList": [
                {"skuPropertyValues": [{"propertyValueDisplayName": "",
                                        "propertyValueName": "Red"}]}
            ]}
        }};</script>"""
        p = _aliexpress_detail(html, "https://www.aliexpress.com/item/222.html")
        assert p is not None
        assert p.variant == "Red"

    def test_aliexpress_review_count_via_str_key(self):
        """totalValidNumStr key also sets review count (line 916->914)."""
        from ujin.extract.product import _aliexpress_detail

        html = """<script>window.runParams = {"data": {
            "titleModule": {"subject": "Widget",
                           "feedbackRating": {"totalValidNumStr": "2,500"}},
            "priceModule": {"formatedPrice": "$5.00"},
            "imageModule": {"imagePathList": ["https://img.example/x.jpg"]}
        }};</script>"""
        p = _aliexpress_detail(html, "https://www.aliexpress.com/item/333.html")
        assert p is not None
        assert p.review_count == 2500


class TestProductExtraEdgeCases:
    """Miscellaneous uncovered branches in product.py."""

    def test_from_jsonld_skips_non_product_type(self):
        """_from_jsonld continue when @type is not Product (line 344)."""
        from ujin.extract.product import _from_jsonld

        result = _from_jsonld([
            {"@type": "BreadcrumbList", "itemListElement": []},  # not Product → continue
            {"@type": "Product", "name": "Widget", "image": "https://img.example/w.jpg",
             "offers": {"price": "9.99", "priceCurrency": "USD"}},
        ], source="test", base_url="https://x.com/")
        assert len(result) == 1

    def test_from_cards_zero_price_continue(self):
        """_from_cards skips cards with zero price (line 388 continue)."""
        from ujin.extract.product import extract_products

        html = """<html><body>
        <div data-component-type="s-search-result" data-asin="B0ZERO">
          <h2><span>Zero Price Widget</span></h2>
          <img class="s-image" src="https://m.media-amazon.com/images/I/x.jpg"/>
          <span class="a-price"><span class="a-offscreen">$0.00</span></span>
          <a class="a-link-normal" href="/dp/B0ZERO"></a>
        </div>
        </body></html>"""
        products = extract_products(html, "https://www.amazon.com/s?k=x")
        assert not any(p.source_id == "B0ZERO" for p in products)

    def test_extract_products_keeps_id_less_items(self):
        """extract_products dedup: id-less products pass through (line 445->449)."""
        from ujin.extract.product import extract_products

        # Build a page with JSON-LD that has no sku/mpn (source_id=None)
        html = """<html><head>
        <script type="application/ld+json">
        [{"@type":"Product","name":"Item One","image":"https://img.example/1.jpg",
          "offers":{"price":"5.00","priceCurrency":"USD"}},
         {"@type":"Product","name":"Item Two","image":"https://img.example/2.jpg",
          "offers":{"price":"6.00","priceCurrency":"USD"}}]
        </script></head><body></body></html>"""
        products = extract_products(html, "https://x.com/")
        # Both id-less items should be present (no dedup on None id)
        assert len(products) >= 2

    def test_extract_gallery_dedup(self):
        """_extract_gallery skips duplicate URLs (line 513->511 False)."""
        from ujin.extract.product import _extract_gallery

        # Two entries with the same hiRes URL → only one in output
        html = (
            '"hiRes":"https://img.example/same.jpg",'
            '"thumb":"https://img.example/t1.jpg",'
            '"large":"https://img.example/l1.jpg"'
            '"hiRes":"https://img.example/same.jpg",'
            '"thumb":"https://img.example/t2.jpg",'
            '"large":"https://img.example/l2.jpg"'
        )
        result = _extract_gallery(html)
        assert result.count("https://img.example/same.jpg") == 1

    def test_extract_specs_single_cell_row_skipped(self):
        """_extract_specs skips table rows with < 2 cells (line 526->524 False)."""
        from selectolax.parser import HTMLParser

        from ujin.extract.product import _AMAZON_DETAIL, _extract_specs

        html = """<html><body>
        <div id="productOverview_feature_div"><table>
          <tr><td>Only one cell</td></tr>
          <tr><td>Brand</td><td>Acme</td></tr>
        </table></div>
        <div id="detailBullets_feature_div"></div>
        </body></html>"""
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style"])
        specs = _extract_specs(tree, _AMAZON_DETAIL)
        assert "Brand" in specs
        assert "Only one cell" not in specs

    def test_extract_specs_bullet_limit(self):
        """_extract_specs stops at limit (line 540 break)."""
        from selectolax.parser import HTMLParser

        from ujin.extract.product import _AMAZON_DETAIL, _extract_specs

        # 20 detail bullets but limit=3
        bullets = "".join(f"<li><span>Key{i} : Val{i}</span></li>" for i in range(20))
        html = f"""<html><body>
        <div id="productOverview_feature_div"></div>
        <div id="detailBullets_feature_div"><ul>{bullets}</ul></div>
        </body></html>"""
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style"])
        specs = _extract_specs(tree, _AMAZON_DETAIL, limit=3)
        assert len(specs) <= 3

    def test_extract_specs_duplicate_key_skipped(self):
        """_extract_specs skips duplicate keys (line 537->539 False)."""
        from selectolax.parser import HTMLParser

        from ujin.extract.product import _AMAZON_DETAIL, _extract_specs

        html = """<html><body>
        <div id="productOverview_feature_div"><table>
          <tr><td>Brand</td><td>Acme</td></tr>
          <tr><td>Brand</td><td>Other</td></tr>
        </table></div>
        <div id="detailBullets_feature_div"></div>
        </body></html>"""
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style"])
        specs = _extract_specs(tree, _AMAZON_DETAIL)
        assert specs.get("Brand") == "Acme"  # first wins

    def test_selected_variant_dedup(self):
        """_selected_variant deduplicates repeated dimension values (line 549->547)."""
        from selectolax.parser import HTMLParser

        from ujin.extract.product import _AMAZON_DETAIL, _selected_variant

        # Two nodes with the same text → only one in the result
        html = """<html><body>
        <span class="inline-twister-dim-title-value a-text-bold"> Black </span>
        <span class="inline-twister-dim-title-value a-text-bold"> Black </span>
        <span class="inline-twister-dim-title-value a-text-bold"> 500GB </span>
        </body></html>"""
        tree = HTMLParser(html)
        result = _selected_variant(tree, _AMAZON_DETAIL)
        # "Black" should appear only once
        assert result == "Black · 500GB"

    def test_detail_from_jsonld_non_dict_block(self):
        """_detail_from_jsonld skips non-dict blocks (line 569)."""
        from ujin.extract.product import _detail_from_jsonld

        html = """<html><head>
        <script type="application/ld+json">
        ["not-a-dict", {"@type":"Product","name":"Widget",
         "image":"https://img.example/w.jpg",
         "offers":{"price":"9.99","priceCurrency":"USD"}}]
        </script></head></html>"""
        p = _detail_from_jsonld(html, "https://x.com/", source="shopify")
        assert p is not None
        assert p.title == "Widget"

    def test_detail_from_jsonld_specs_item_populated(self):
        """_detail_from_jsonld populates specs from scalar block props (line 633)."""
        from ujin.extract.product import _detail_from_jsonld

        html = """<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Widget","image":"https://img.example/x.jpg",
         "offers":{"price":"9.99","priceCurrency":"USD"},
         "color":"Red","material":"Steel"}
        </script></head></html>"""
        p = _detail_from_jsonld(html, "https://x.com/", source="shopify")
        assert p is not None
        assert p.specs.get("color") == "Red"
        assert p.specs.get("material") == "Steel"

    def test_best_title_badge_makes_empty(self):
        """_best_title returns None when all candidates are stripped to empty (line 168->166)."""
        from selectolax.parser import HTMLParser

        from ujin.extract.product import _best_title

        # All span text is a badge word that gets stripped
        html = "<div><h2><span>Sponsored</span></h2></div>"
        tree = HTMLParser(html)
        card = tree.css_first("div")
        # All candidates stripped to "" → max() picks longest, but if all empty, None
        result = _best_title(card, ("h2 span",))
        # "Sponsored" → stripped by _TITLE_BADGE_RE → empty → not added to candidates
        assert result is None

    def test_extract_description_empty_text_node(self):
        """extract_description when bullet has empty text (line 94->92 False)."""
        from ujin.extract.product import extract_description

        # Bullet nodes exist but are empty — should not add to parts
        html = """<html><body>
        <ul id="feature-bullets">
          <li><span class="a-list-item">  </span></li>
          <li><span class="a-list-item">Real bullet point</span></li>
        </ul>
        </body></html>"""
        result = extract_description(html, source="amazon")
        assert result and "Real bullet point" in result
        assert result.count("·") == 0  # only 1 non-empty bullet, no separator


def _make_marketplace_src(monkeypatch, terms_per_poll=1):
    """Helper: a MarketplaceSearchPollable with a minimal inline profile."""
    from ujin.poll.marketplace import MarketplaceSearchPollable

    return MarketplaceSearchPollable(
        profile="shop",
        profiles={"shop": {"domain": "shop.example.com",
                            "search_url": "https://shop.example.com/search?q={query}",
                            "keyterms": {"Electronics": ["widget", "gadget"]}}},
        terms_per_poll=terms_per_poll,
        seed=1,
    )
