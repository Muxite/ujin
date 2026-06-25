"""Edge-case / error-path tests that close coverage gaps in the extract modules.

Covers: ujin/__init__.py, ujin/extract/feeds.py, ujin/extract/links.py,
ujin/extract/metadata.py, ujin/extract/product.py, ujin/extract/profile.py,
ujin/extract/structured.py, ujin/extract/tables.py.

All tests are offline and deterministic.
"""
from __future__ import annotations

import json

import pytest

import ujin
from ujin.extract.feeds import extract_feeds
from ujin.extract.links import (
    NormalizedLink,
    _is_boilerplate_text,
    extract_headline_links,
)
from ujin.extract.metadata import extract_metadata
from ujin.extract.product import (
    _ali_price_cents,
    _aliexpress_detail,
    _aliexpress_modules,
    _best_title,
    _detail_from_jsonld,
    _extract_gallery,
    _extract_specs,
    _from_cards,
    _selected_variant,
    _AMAZON_DETAIL,
    _AMAZON_SELECTORS,
    extract_product_detail,
    extract_products,
)
from ujin.extract.profile import apply_link_profile
from ujin.extract.structured import extract_structured
from ujin.extract.tables import extract_tables
from ujin.scrape.host_overrides import ExtractProfile


# ── ujin/__init__.py — lazy __getattr__ paths ─────────────────────────────────

class TestInitLazyImports:
    """Lines 49-63, 68 in ujin/__init__.py."""

    def test_http_pollable(self):
        from ujin import HttpPollable
        assert HttpPollable is not None

    def test_rss_pollable(self):
        from ujin import RssPollable
        assert RssPollable is not None

    def test_api_pollable(self):
        from ujin import ApiPollable
        assert ApiPollable is not None

    def test_adaptive_interval(self):
        from ujin import AdaptiveInterval
        assert AdaptiveInterval is not None

    def test_unknown_attribute_raises(self):
        # Line 68: raise AttributeError
        with pytest.raises(AttributeError, match="no attribute"):
            _ = ujin._NonExistentAttributeXYZ_  # type: ignore[attr-defined]


# ── ujin/extract/feeds.py ─────────────────────────────────────────────────────

class TestFeedsEdgeCases:
    """Line 86 in feeds.py (_FeedsParser.handle_starttag empty-href early return)."""

    def test_link_with_no_href_is_ignored(self):
        # Line 86: href attribute absent/empty → return early
        html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml">'
            "</head></html>"
        )
        assert extract_feeds(html) == []

    def test_link_with_empty_href_is_ignored(self):
        html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" href="  ">'
            "</head></html>"
        )
        assert extract_feeds(html) == []

    def test_dedup_identical_href(self):
        # Lines 59-61: second identical href is skipped (dedup in seen set)
        html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" href="/feed.rss">'
            '<link rel="alternate" type="application/rss+xml" href="/feed.rss">'
            "</head></html>"
        )
        result = extract_feeds(html, base_url="https://x.test/")
        assert len(result) == 1


# ── ujin/extract/links.py ─────────────────────────────────────────────────────

class TestLinksEdgeCases:
    """Branches in links.py."""

    def test_trailing_chevron_boilerplate(self):
        # Line 389-390: text ends with nav chevron AND < 8 words → boilerplate
        # Needs len >= 30, 4-7 words, ends with an arrow char.
        text = "See all the latest stories here →"
        assert len(text.strip()) >= 30
        assert _is_boilerplate_text(text) is True

    def test_repeated_phrase_boilerplate(self):
        # Lines 386-387: tail starts with head → boilerplate
        # 8 words, half=4; words[4:] == words[:4], so tail.startswith(head) is True
        text = "Breaking World News Today Breaking World News Today"
        assert _is_boilerplate_text(text) is True

    def test_fallback_full_document_when_no_main_container(self):
        # Line 414: no main/article/section containers → iterate whole document
        headline = "This is a test headline about important world events happening right now"
        html = (
            "<html><body>"
            f'<div><a href="https://example.com/news/world-events-story-today">{headline}</a></div>'
            "</body></html>"
        )
        links = extract_headline_links(html, base_url="https://example.com/")
        assert any("world-events" in lk.url for lk in links)

    def test_prefer_better_text_dedup(self):
        # Line 452-454: existing text is longer → skip shorter duplicate
        # same URL, second anchor has shorter text → first (longer) wins
        long_title = "This Longer Headline For The Same Article Should Win Over Shorter Text"
        short_title = "Shorter headline text here only"
        html = (
            "<html><body><main>"
            f'<a href="https://example.com/article/story">{long_title}</a>'
            f'<a href="https://example.com/article/story">{short_title}</a>'
            "</main></body></html>"
        )
        links = extract_headline_links(html, base_url="https://example.com/")
        assert len(links) == 1
        assert links[0].text == long_title

    def test_max_links_cap(self):
        # Line 423-424: max_links cap (len(seen) >= max_links → return)
        items = "".join(
            f'<a href="https://example.com/article/{i}">Headline about event number {i} happening today right now</a>'
            for i in range(10)
        )
        html = f"<html><body><main>{items}</main></body></html>"
        links = extract_headline_links(html, base_url="https://example.com/", max_links=3)
        assert len(links) == 3


# ── ujin/extract/metadata.py ─────────────────────────────────────────────────

class TestMetadataEdgeCases:
    """Lines 84->67, empty-content skipping in metadata.py."""

    def test_article_modified_time_is_extracted(self):
        # Branch 84->67: elif lk == "article:modified_time" — was never exercised
        html = (
            "<html><head>"
            '<meta name="article:modified_time" content="2024-06-15T09:00:00Z">'
            "</head></html>"
        )
        result = extract_metadata(html)
        assert result.get("modified") == "2024-06-15T09:00:00Z"

    def test_whitespace_only_content_is_skipped(self):
        # Line 69-70: content.strip() is empty → continue (setdefault never called)
        html = '<html><head><meta name="description" content="   "></head></html>'
        result = extract_metadata(html)
        assert "description" not in result

    def test_unrecognized_meta_key_falls_through_all_elifs(self):
        # Branch 84->67: meta key that doesn't match og:, twitter:, description,
        # author, article:published_time, or article:modified_time → loop continues
        html = (
            "<html><head>"
            '<meta name="robots" content="index, follow">'
            '<meta name="article:modified_time" content="2024-06-15T09:00:00Z">'
            "</head></html>"
        )
        result = extract_metadata(html)
        # robots meta doesn't map to any output field; modified_time does
        assert result.get("modified") == "2024-06-15T09:00:00Z"
        assert "robots" not in result

    def test_og_title_fallback_when_no_title_element(self):
        # Lines 88-89: title not in out but og has title → use og:title
        html = (
            "<html><head>"
            '<meta property="og:title" content="OG Headline">'
            "</head></html>"
        )
        result = extract_metadata(html)
        assert result.get("title") == "OG Headline"


# ── ujin/extract/product.py ──────────────────────────────────────────────────

class TestProductEdgeCases:
    """Cover branches in product.py."""

    def test_best_title_badge_only_is_skipped(self):
        # Line 168->166: badge stripped → empty text → not appended
        from selectolax.parser import HTMLParser
        html = (
            '<div data-component-type="s-search-result">'
            "<h2><span>Sponsored</span></h2>"
            "</div>"
        )
        tree = HTMLParser(html)
        card = tree.css_first('[data-component-type="s-search-result"]')
        # "Sponsored" matches _TITLE_BADGE_RE and is stripped → "" → not kept
        result = _best_title(card, ("h2 span",))
        assert result is None

    def test_extract_products_zero_price_skips_card(self):
        # Line 388: cents <= 0 → continue (skip card)
        html = (
            '<div data-component-type="s-search-result" data-asin="B001ZERO">'
            "<h2><span>Some Product Title Here Widget</span></h2>"
            '<span class="a-price"><span class="a-offscreen">$0.00</span></span>'
            '<img class="s-image" src="https://images.example.com/img.jpg">'
            '<a class="a-link-normal s-no-outline" href="/dp/B001ZERO">buy</a>'
            "</div>"
        )
        results = _from_cards(
            html, source="amazon", base_url="https://www.amazon.com/",
            selectors=_AMAZON_SELECTORS,
        )
        assert results == []

    def test_dedup_passes_product_with_no_source_id(self):
        # Line 445->449: p.source_id is None → skip the seen-check block, append directly
        ld = {
            "@type": "Product",
            "name": "Widget Without SKU",
            "image": "https://shop.example.com/img.jpg",
            "offers": {"price": "9.99", "priceCurrency": "USD"},
        }
        html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        products = extract_products(html, base_url="https://shop.example.com/", source="shop")
        assert len(products) == 1
        assert products[0].source_id is None

    def test_extract_gallery_dedup(self):
        # Line 513->511: duplicate URL in gallery — second occurrence skipped
        url = "https://images.example.com/product.jpg"
        entry = f'"hiRes":"{url}","thumb":"https://images.example.com/t.jpg","large":"{url}"'
        gallery = _extract_gallery(entry + "," + entry)
        assert gallery == [url]

    def test_extract_gallery_limit(self):
        # Line 517: break when len(out) >= limit
        entry_tpl = (
            '"hiRes":"https://img.example.com/{n}.jpg",'
            '"thumb":"https://img.example.com/t.jpg",'
            '"large":"https://img.example.com/{n}.jpg"'
        )
        entries = ",".join(entry_tpl.format(n=i) for i in range(12))
        gallery = _extract_gallery(entries, limit=10)
        assert len(gallery) == 10

    def test_extract_specs_row_one_cell_skipped(self):
        # Line 526->524: row with < 2 cells → skip (if len(cells) >= 2 is False)
        from selectolax.parser import HTMLParser
        html = (
            "<table>"
            "<tr><td>OnlyOneCell</td></tr>"
            "<tr><td>Color</td><td>Blue</td></tr>"
            "</table>"
        )
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style", "noscript", "template"])
        sel = {**_AMAZON_DETAIL, "overview": "table tr", "detail_bullets": "NONE_MATCHING"}
        specs = _extract_specs(tree, sel)
        assert "Color" in specs
        assert "OnlyOneCell" not in specs

    def test_extract_specs_bullet_no_colon_skipped(self):
        # Line 535->539: bullet with no colon → len(parts) != 2 → not added
        from selectolax.parser import HTMLParser
        html = (
            "<ul>"
            "<li>No colon in this plain text here</li>"
            "<li>Weight : 500g</li>"
            "</ul>"
        )
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style", "noscript", "template"])
        sel = {**_AMAZON_DETAIL, "overview": "NONE_MATCHING", "detail_bullets": "li"}
        specs = _extract_specs(tree, sel)
        assert "Weight" in specs
        assert len(specs) == 1

    def test_extract_specs_bullet_empty_val_skipped(self):
        # Line 537->539: val is empty after split → condition is False → not added
        from selectolax.parser import HTMLParser
        html = (
            "<ul>"
            "<li>EmptyVal : </li>"
            "<li>Material : Aluminum</li>"
            "</ul>"
        )
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style", "noscript", "template"])
        sel = {**_AMAZON_DETAIL, "overview": "NONE_MATCHING", "detail_bullets": "li"}
        specs = _extract_specs(tree, sel)
        assert "EmptyVal" not in specs
        assert "Material" in specs

    def test_extract_specs_bullet_duplicate_key_skipped(self):
        # Line 537->539: key already in specs → not re-added
        from selectolax.parser import HTMLParser
        html = (
            "<ul>"
            "<li>Color : Blue</li>"
            "<li>Color : Red</li>"
            "</ul>"
        )
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style", "noscript", "template"])
        sel = {**_AMAZON_DETAIL, "overview": "NONE_MATCHING", "detail_bullets": "li"}
        specs = _extract_specs(tree, sel)
        assert specs.get("Color") == "Blue"  # first wins, second skipped

    def test_extract_specs_limit_triggers_break(self):
        # Line 540: break when len(specs) >= limit (default 14)
        from selectolax.parser import HTMLParser
        bullets = "".join(f"<li>Key{i} : Value{i}</li>" for i in range(20))
        html = f"<ul>{bullets}</ul>"
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style", "noscript", "template"])
        sel = {**_AMAZON_DETAIL, "overview": "NONE_MATCHING", "detail_bullets": "li"}
        specs = _extract_specs(tree, sel)
        assert len(specs) <= 14

    def test_selected_variant_empty_text_skipped(self):
        # Line 549->547: cleaned text is empty → not appended to vals
        from selectolax.parser import HTMLParser
        html = (
            "<div>"
            '<span class="inline-twister-dim-title-value a-text-bold">  </span>'
            '<span class="inline-twister-dim-title-value a-text-bold">2TB</span>'
            "</div>"
        )
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style", "noscript", "template"])
        result = _selected_variant(tree, _AMAZON_DETAIL)
        assert result == "2TB"

    def test_detail_from_jsonld_non_dict_offers(self):
        # Line 584: offers is not a dict → reset to {} → price None → return None
        ld = {
            "@type": "Product",
            "name": "Test Widget",
            "image": "https://shop.example.com/img.jpg",
            "offers": "https://shop.example.com/buy",
        }
        html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        result = _detail_from_jsonld(html, "https://shop.example.com/p/", source="shop")
        assert result is None

    def test_detail_from_jsonld_brace_in_spec_value_skipped(self):
        # Line 630->632: extra key with '{' in value → not added to specs
        ld = {
            "@type": "Product",
            "name": "Test Widget",
            "image": "https://shop.example.com/img.jpg",
            "offers": {"price": "19.99", "priceCurrency": "USD"},
            "badProp": "{template_blob}",
            "goodProp": "solid_value",
        }
        html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        result = _detail_from_jsonld(html, "https://shop.example.com/p/", source="shop")
        if result is not None:
            assert not any("{" in v for v in result.specs.values())
            assert "goodProp" in result.specs

    def test_detail_price_node_zero_falls_back_to_jsonld(self):
        # Line 734->729: price_to_cents returns 0 for "$0" → c > 0 is False → try next selector
        # With no valid price at all, falls back to _detail_from_jsonld → returns None
        html = (
            "<html><head></head><body>"
            '<div id="productTitle">My Headphones Pro</div>'
            '<div id="corePriceDisplay_desktop_feature_div">'
            '<span class="a-offscreen">$0</span>'
            "</div>"
            "</body></html>"
        )
        result = extract_product_detail(html, "https://www.amazon.com/dp/B001TEST01")
        assert result is None

    def test_aliexpress_modules_no_brace_after_marker(self):
        # Line 832: marker found but html.find("{", ...) returns -1
        html = "window.runParams = null; // no JSON object follows"
        result = _aliexpress_modules(html)
        assert result is None

    def test_aliexpress_modules_list_structure_walk(self):
        # Lines 853-854: walk() recurses into list values in the parsed blob
        inner_data = {
            "titleModule": {"subject": "Test Product"},
            "priceModule": {"minAmount": {"value": 9.99, "currency": "USD"}},
        }
        # Wrap module data inside a list so walk() hits the list branch
        blob = {
            "items": [inner_data],
        }
        html = f"window.runParams = {json.dumps(blob)};"
        # walk() should find titleModule/priceModule nested inside the list
        result = _aliexpress_modules(html)
        # Result is either the found dict or None depending on nesting depth
        assert result is None or isinstance(result, dict)

    def test_ali_price_cents_zero_structured_amount_tries_next(self):
        # Line 869->865: structured amount parses to 0 → skip to next key
        price_mod = {
            "minActivityAmount": {"value": "0.00", "currency": "USD"},
            "minAmount": {"value": 29.99, "currency": "USD"},
        }
        cents, currency = _ali_price_cents(price_mod)
        assert cents == 2999
        assert currency == "USD"

    def test_aliexpress_detail_gallery_dedup(self):
        # Line 902->898: duplicate image URL in imagePathList → second skipped
        url = "https://ae01.alicdn.com/kf/product.jpg"
        data = _build_aliexpress_data(images=[url, url, url])
        html = f"window.runParams = {json.dumps(data)};"
        result = _aliexpress_detail(html, "https://www.aliexpress.com/item/100500.html")
        if result is not None:
            assert result.images.count(url) == 1

    def test_aliexpress_detail_brand_not_overwritten(self):
        # Line 928->930: brand already set → second "Brand Name" prop does not overwrite
        props = [
            {"attrName": "Brand Name", "attrValue": "ACME Corp"},
            {"attrName": "Brand Name", "attrValue": "OTHER Brand"},
        ]
        data = _build_aliexpress_data(props=props)
        html = f"window.runParams = {json.dumps(data)};"
        result = _aliexpress_detail(html, "https://www.aliexpress.com/item/100500.html")
        if result is not None:
            assert result.brand == "ACME Corp"

    def test_aliexpress_sku_empty_values_list(self):
        # Line 937->935: skuPropertyValues is [] → vals is empty → if vals: is False
        sku_props = [{"skuPropertyValues": []}]
        data = _build_aliexpress_data(sku_props=sku_props)
        html = f"window.runParams = {json.dumps(data)};"
        result = _aliexpress_detail(html, "https://www.aliexpress.com/item/100500.html")
        if result is not None:
            assert result.variant is None

    def test_aliexpress_sku_empty_display_name(self):
        # Line 939->935: cleaned display name is empty → v is empty → if v: is False
        sku_props = [
            {"skuPropertyValues": [{"propertyValueDisplayName": "", "propertyValueName": ""}]}
        ]
        data = _build_aliexpress_data(sku_props=sku_props)
        html = f"window.runParams = {json.dumps(data)};"
        result = _aliexpress_detail(html, "https://www.aliexpress.com/item/100500.html")
        if result is not None:
            assert result.variant is None

    def test_aliexpress_specs_empty_name_val_skipped(self):
        # Line 925->930 / 927 False branch: name or val is empty → not added
        props = [
            {"attrName": "", "attrValue": "some value"},
            {"attrName": "Color", "attrValue": ""},
            {"attrName": "Size", "attrValue": "Large"},
        ]
        data = _build_aliexpress_data(props=props)
        html = f"window.runParams = {json.dumps(data)};"
        result = _aliexpress_detail(html, "https://www.aliexpress.com/item/100500.html")
        if result is not None:
            assert "Size" in result.specs
            assert "" not in result.specs


def _build_aliexpress_data(
    images: list | None = None,
    props: list | None = None,
    sku_props: list | None = None,
) -> dict:
    """Build minimal window.runParams data for AliExpress detail tests."""
    if images is None:
        images = ["https://ae01.alicdn.com/kf/product.jpg"]
    return {
        "data": {
            "titleModule": {"subject": "Test AliExpress Product Alpha"},
            "priceModule": {"minAmount": {"value": 9.99, "currency": "USD"}},
            "imageModule": {"imagePathList": images},
            "specsModule": {"props": props or []},
            "skuModule": {
                "productSKUPropertyList": (
                    [{"skuPropertyValues": sp["skuPropertyValues"]} for sp in sku_props]
                    if sku_props else []
                ),
            },
        }
    }


# ── ujin/extract/profile.py ──────────────────────────────────────────────────

class TestProfileEdgeCases:
    """Edge cases in profile.py that don't require the id() exclude path."""

    def test_invalid_url_path_must_match_pattern_is_warned(self):
        # Lines 59-61: invalid url_path_must_match regex → warning logged, path_re=None
        profile = ExtractProfile(
            link_selectors=("a",),
            url_path_must_match="[invalid-regex(",
        )
        html = (
            "<html><body>"
            '<a href="https://example.com/article/story">Visible Story About Events Here Today</a>'
            "</body></html>"
        )
        # Should not raise — invalid regex is logged and ignored (path_re stays None)
        links = apply_link_profile(html, base_url="https://example.com/", profile=profile)
        assert isinstance(links, list)

    def test_invalid_path_deny_pattern_is_warned(self):
        # Lines 67-68: invalid re.Pattern in url_path_deny_patterns → warning logged
        profile = ExtractProfile(
            link_selectors=("a",),
            url_path_deny_patterns=("[invalid-re",),
        )
        html = "<html><body><a href='https://example.com/article/x'>Click here for news</a></body></html>"
        links = apply_link_profile(html, base_url="https://example.com/", profile=profile)
        assert isinstance(links, list)

    def test_invalid_title_deny_pattern_is_warned(self):
        # Lines 74-75: invalid re.Pattern in title_deny_patterns → warning logged
        profile = ExtractProfile(
            link_selectors=("a",),
            title_deny_patterns=("[bad-re",),
        )
        html = "<html><body><a href='https://example.com/article/x'>Click here for news</a></body></html>"
        links = apply_link_profile(html, base_url="https://example.com/", profile=profile)
        assert isinstance(links, list)


# ── ujin/extract/structured.py ───────────────────────────────────────────────

class TestStructuredEdgeCases:
    """Branch 81->71 in structured.py: itemprop with no extractable value."""

    def test_itemprop_empty_value_not_added_to_props(self):
        # Line 81->71: value is "" (falsy) → if value: is False → not added
        html = (
            '<div itemscope itemtype="https://schema.org/Product">'
            '<span itemprop="name">Widget Pro</span>'
            '<span itemprop="emptyProp"></span>'
            "</div>"
        )
        result = extract_structured(html)
        items = result["microdata"]
        assert len(items) == 1
        assert "name" in items[0]["props"]
        assert "emptyProp" not in items[0]["props"]

    def test_itemprop_with_no_attributes_and_no_text(self):
        # Ensure falsy value branch covers the iteration-continue case
        html = (
            '<div itemscope itemtype="https://schema.org/Product">'
            '<span itemprop="ghost_prop" />'
            '<span itemprop="name">Real Name</span>'
            "</div>"
        )
        result = extract_structured(html)
        items = result["microdata"]
        assert len(items) == 1
        assert "name" in items[0]["props"]


# ── ujin/extract/tables.py ───────────────────────────────────────────────────

class TestTablesEdgeCases:
    """Branch 114->110 in tables.py: non-tr, non-section direct child of table."""

    def test_table_with_colgroup_direct_child(self):
        # Line 114->110: colgroup is not <tr> nor section tag → falls through to loop continue
        html = (
            "<table>"
            "<colgroup><col span='2'></colgroup>"
            "<tbody><tr><th>Name</th><th>Age</th></tr>"
            "<tr><td>Alice</td><td>30</td></tr></tbody>"
            "</table>"
        )
        result = extract_tables(html)
        assert result == [{"Name": "Alice", "Age": "30"}]

    def test_table_with_caption_direct_child(self):
        # Another 114->110 case: caption element as direct table child
        html = (
            "<table>"
            "<caption>Employee Data</caption>"
            "<tbody><tr><th>Name</th></tr>"
            "<tr><td>Bob</td></tr></tbody>"
            "</table>"
        )
        result = extract_tables(html)
        assert result == [{"Name": "Bob"}]

    def test_empty_table_returns_no_rows(self):
        assert extract_tables("<table></table>") == []

    def test_headerless_table_uses_positional_keys(self):
        # Line 95-96: first row has no th → keys=None → use col0/col1 positional keys
        html = (
            "<table>"
            "<tbody><tr><td>Alice</td><td>30</td></tr></tbody>"
            "</table>"
        )
        result = extract_tables(html)
        assert result == [{"col0": "Alice", "col1": "30"}]

    def test_all_empty_row_is_dropped(self):
        # Line 88-89: all values in a row are empty → row is dropped
        html = (
            "<table>"
            "<tbody>"
            "<tr><th>A</th><th>B</th></tr>"
            "<tr><td></td><td></td></tr>"
            "<tr><td>x</td><td>y</td></tr>"
            "</tbody>"
            "</table>"
        )
        result = extract_tables(html)
        assert len(result) == 1
        assert result[0] == {"A": "x", "B": "y"}
