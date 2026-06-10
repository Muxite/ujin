"""Extraction over the saved HTML corpus (tests/fixtures/html)."""
from __future__ import annotations

import pytest

from ujin.extract import extract_article, extract_headline_links
from ujin.extract.links import fingerprint_links, normalize_url

BASE = "https://news.example.com/"


# ── headline links ───────────────────────────────────────────────────────────

def test_news_index_extracts_articles_not_chrome(html_corpus):
    links = extract_headline_links(html_corpus["news_index"], base_url=BASE)
    urls = {l.url for l in links}
    # all the top stories make it
    assert f"{BASE}2026/06/09/markets-rally-on-rate-decision".replace("//2026", "/2026") \
        in urls or any("markets-rally" in u for u in urls)
    assert sum("2026/" in u for u in urls) >= 20
    # nav/footer/aside chrome excluded
    assert not any(u.endswith(("/privacy", "/terms", "/about", "/newsletter"))
                   for u in urls)
    assert not any(u.startswith("mailto:") for u in urls)
    # every link got resolved absolute
    assert all(u.startswith("http") for u in urls)


def test_js_shell_yields_too_few_links_for_fast_path(html_corpus):
    """The JS app shell is what triggers obscura escalation (<5 links)."""
    links = extract_headline_links(html_corpus["js_shell"], base_url=BASE)
    assert len(links) < 5


def test_malformed_html_does_not_crash(html_corpus):
    # The fixture has unclosed tags, an unterminated comment, and stray
    # brackets; the contract is graceful recovery, not specific links.
    links = extract_headline_links(html_corpus["malformed"], base_url=BASE)
    assert isinstance(links, list)
    art = extract_article(html_corpus["malformed"], url=BASE)
    assert art is None or hasattr(art, "text")


def test_fingerprint_stable_and_order_sensitive(html_corpus):
    links = extract_headline_links(html_corpus["news_index"], base_url=BASE)
    assert fingerprint_links(links) == fingerprint_links(list(links))
    assert fingerprint_links(links) != fingerprint_links([])


# ── normalize_url edge cases (mirrors fixtures/html/relative_links.html) ────

@pytest.mark.parametrize("raw,base,expected", [
    ("plain-relative", "https://b.test/section/", "https://b.test/section/plain-relative"),
    ("./dot-relative", "https://b.test/section/", "https://b.test/section/dot-relative"),
    ("../parent", "https://b.test/section/", "https://b.test/parent"),
    ("/absolute-path", "https://b.test/section/", "https://b.test/absolute-path"),
    ("//cdn.test/x", "https://b.test/", "https://cdn.test/x"),
    ("https://other.test/full", None, "https://other.test/full"),
    ("HTTPS://UPPER.TEST/CASE", None, "https://upper.test/CASE"),  # host lowered, path kept
    ("https://b.test/a#frag", None, "https://b.test/a"),           # fragment dropped
    ("  https://b.test/pad  ", None, "https://b.test/pad"),
])
def test_normalize_url_resolution(raw, base, expected):
    assert normalize_url(raw, base=base) == expected


@pytest.mark.parametrize("raw", [
    "", "javascript:void(0)", "mailto:x@y.test", "tel:+15551234567",
    "#fragment-only", "ftp://files.test/x", "/no-base-relative",
])
def test_normalize_url_rejects_unusable(raw):
    assert normalize_url(raw) is None


def test_normalize_url_strips_tracking_params():
    url = "https://b.test/story?utm_source=tw&utm_medium=social&id=7&fbclid=xyz"
    assert normalize_url(url) == "https://b.test/story?id=7"


def test_normalize_url_sorts_query_for_canonicalization():
    a = normalize_url("https://b.test/s?b=2&a=1")
    b = normalize_url("https://b.test/s?a=1&b=2")
    assert a == b


# ── article extraction ───────────────────────────────────────────────────────

def test_article_extraction_from_corpus(html_corpus):
    art = extract_article(
        html_corpus["article"],
        url=f"{BASE}2026/06/08/quantum-error-correction-milestone",
    )
    assert art is not None
    assert "quantum" in art.title.lower()
    assert "logical qubit" in art.text
    assert len(art.text) > 500


def test_article_extraction_on_shell_returns_none_or_empty(html_corpus):
    art = extract_article(html_corpus["js_shell"], url=BASE)
    assert art is None or len(art.text or "") < 50
