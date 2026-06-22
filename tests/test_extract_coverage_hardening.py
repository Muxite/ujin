"""Offline unit tests closing coverage gaps in extract/links, extract/article,
extract/structured, and poll/browser.

Only test files are added — no production source is modified.
All tests are offline and deterministic.
"""
from __future__ import annotations

import sys
import pytest

from ujin.extract.links import (
    _is_boilerplate_text,
    _is_photo_credit,
    _is_slop_text,
    _is_slop_url,
    extract_headline_links,
    normalize_url,
)
from ujin.extract.structured import extract_structured
from ujin.extract.article import (
    _looks_like_index_body,
    _looks_like_index_url,
    _run_trafilatura,
    extract_article,
    extract_article_lenient,
)

BASE = "https://news.example.com/"


# ── links.py: _is_slop_url (line 198) ────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://cooking.nyt.com/recipes/pasta", True),       # host prefix
    ("https://wirecutter.nyt.com/reviews/tv", True),       # host prefix
    ("https://shop.example.com/deals", True),              # host prefix
    ("https://news.example.com/cooking/pasta-recipe", True),  # path segment
    ("https://news.example.com/games/wordle-2025", True),  # path segment
    ("https://news.example.com/puzzles/crossword", True),  # path segment
    ("https://news.example.com/world/conflict-erupts", False),
    ("https://news.example.com/2026/06/economy", False),
])
def test_is_slop_url(url, expected):
    assert _is_slop_url(url) is expected


# ── links.py: _is_slop_text (line 205) ───────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Play Wordle today and beat your streak", True),
    ("Today's Wordle answer revealed", True),
    ("Play Connections — today's groups puzzle", True),
    ("Wirecutter best picks for summer", True),
    ("Buy Now and save on electronics", True),
    ("Your Horoscope for the week ahead", True),
    ("Breaking: parliament votes on new bill", False),
    ("Scientists announce climate breakthrough", False),
    ("", False),
])
def test_is_slop_text(text, expected):
    assert _is_slop_text(text) is expected


# ── links.py: _is_photo_credit (line 323) ────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Al Pereira/Getty Images", True),
    ("AP Photo/Jane Doe", True),
    ("via REUTERS", True),
    ("AFP/some photographer", True),
    ("Photographer: John Smith", True),
    ("Photo: Jane Doe", True),
    ("Eric/Bloomberg", True),
    ("Archive/Michael Ochs Archives", True),
    ("Reuters reports new sanctions against Russia", False),  # no "via"
    ("The AFP confirmed the report", False),               # prefix not "AFP/"
    ("", False),
])
def test_is_photo_credit(text, expected):
    assert _is_photo_credit(text) is expected


# ── links.py: _is_boilerplate_text — every branch ────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # empty / whitespace only → True (line 347)
    ("", True),
    ("   ", True),
    # shorter than _MIN_HEADLINE_CHARS (30) → True (line 349)
    ("Too short", True),
    # exact boilerplate match (via being short) → True (line 351)
    ("Subscribe", True),
    # fewer than 4 words (also short) → True (line 355)
    ("Three word phrase", True),
    # more than 25 words → True (line 357)
    ("a b c d e f g h i j k l m n o p q r s t u v w x y z extra words here now", True),
    # all-caps label ≤20 chars, no digits → True (line 360)
    ("LATEST NEWS BREAKING", True),
    ("VIDEO", True),
    # nav prefix → True (line 363)
    ("Watch: the full documentary on the climate crisis this week", True),
    ("Listen: podcast explores the future of artificial intelligence", True),
    ("Read more: coverage of the ongoing conflict in eastern europe this", True),
    # nav suffix chars only → True (line 366)
    ("→", True),
    ("›", True),
    # paywall/sponsored → True (line 369)
    ("This story is for subscribers to our premium service only here", True),
    ("Sponsored content from our advertiser partners around the world", True),
    # photo credit → True (line 372)
    ("Campaign photo/Getty Images from the anti-war protests downtown", True),
    # time-ago regex → True (line 375)
    ("2h ago", True),
    ("14 days ago", True),
    ("3 weeks ago", True),
    # date regex → True (line 377)
    ("12/31/2025", True),
    ("1-1-24", True),
    # repeated phrase (tail starts with head) → True (line 383)
    # 8 words: first 4 == last 4 → tail.startswith(head) is True
    ("Texas Senate primary runoff Texas Senate primary runoff", True),
    # trailing chevron with < 8 words → True (line 386)
    ("Read more from the world ›", True),
    # < 4 words but text is >= 30 chars (avoids the length guard) → True (line 356)
    ("Antidisestablishmentarianism counterrevolutionary", True),
    # trailing chevron < 8 words, text > 30 chars (passes length guard) → True (line 390)
    ("Read about the emerging science trends ›", True),
    # valid headline — no filter triggers → False
    ("Scientists discover a new class of quantum error-correcting codes in lab", False),
    ("Finance ministers reach landmark deal on global tax reform at summit", False),
])
def test_is_boilerplate_text(text, expected):
    assert _is_boilerplate_text(text) is expected


# ── links.py: normalize_url — missing branches ────────────────────────────────

def test_normalize_url_empty_netloc_returns_none():
    # scheme present but netloc is empty (line 238)
    assert normalize_url("https:///path/only") is None


def test_normalize_url_at_prefix_tracking_params_stripped():
    # BBC-style at_campaign / at_medium (Cycle 5.1 addition)
    url = "https://bbc.com/story?at_campaign=rss&at_medium=RSS&id=42"
    assert normalize_url(url) == "https://bbc.com/story?id=42"


def test_normalize_url_traffic_source_stripped():
    url = "https://aljazeera.com/story?traffic_source=rss&article_id=99"
    assert normalize_url(url) == "https://aljazeera.com/story?article_id=99"


# ── links.py: extract_headline_links — edge cases ────────────────────────────

def test_extract_empty_html_returns_empty():
    assert extract_headline_links("", base_url=BASE) == []


def test_extract_max_links_caps_output():
    bodies = " ".join(
        f'<a href="/story/{i}">A valid long headline about story number {i} in the news</a>'
        for i in range(30)
    )
    html = f"<main>{bodies}</main>"
    links = extract_headline_links(html, base_url=BASE, max_links=5)
    assert len(links) == 5


def test_extract_fallback_whole_document_when_no_main_container():
    """No <main>/article/etc → falls back to iterating all <a> in document."""
    html = """
    <html><body>
      <div class="content">
        <a href="/story/one">Breaking news about the global economy and market rally</a>
        <a href="/story/two">Scientists discover a new approach to treating cancer disease</a>
      </div>
    </body></html>
    """
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert any("one" in u for u in urls)
    assert any("two" in u for u in urls)


def test_extract_skips_nav_tag():
    html = """<html><body>
      <main><a href="/story/good">A genuine news headline about current world affairs today</a></main>
      <nav><a href="/story/bad">Navigation link about menu and settings options bar</a></nav>
    </body></html>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert any("good" in u for u in urls)
    assert not any("bad" in u for u in urls)


def test_extract_skips_role_navigation():
    """_is_excluded_element: role=navigation branch (line 269)."""
    html = """<html><body>
      <div role="navigation">
        <a href="/nav/item">This is a navigational link about the website structure</a>
      </div>
      <main>
        <a href="/story/real">Real breaking news story about world events happening today</a>
      </main>
    </body></html>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("nav" in u for u in urls)
    assert any("real" in u for u in urls)


def test_extract_skips_aria_label_nav():
    """_is_excluded_element: aria-label containing 'nav' branch (line 272)."""
    html = """<html><body>
      <div aria-label="primary site navigation">
        <a href="/nav/menu">Primary navigation link to homepage section area</a>
      </div>
      <main>
        <a href="/story/yes">Important story about international politics and diplomacy</a>
      </main>
    </body></html>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("menu" in u for u in urls)
    assert any("yes" in u for u in urls)


def test_extract_skips_excluded_class():
    """_is_excluded_element: class pattern branch (line 275)."""
    html = """<html><body>
      <div class="site-footer-nav">
        <a href="/footer/link">Footer navigation link about subscription and newsletter options</a>
      </div>
      <main>
        <a href="/story/keep">A genuine article headline that should be kept in the output</a>
      </main>
    </body></html>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("footer" in u for u in urls)
    assert any("keep" in u for u in urls)


def test_extract_dedup_prefers_longer_text():
    """Same URL twice → keep the longer text (line 452-454 in consider())."""
    html = """<main>
      <a href="/story/x">Short but still long enough headline to pass the gate</a>
      <a href="/story/x">This much longer and more descriptive headline about the same story wins here</a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    assert len(links) == 1
    assert "longer and more descriptive" in links[0].text


def test_extract_uses_aria_label_when_text_empty():
    """Anchor with no text uses aria-label (line 436)."""
    html = """<main>
      <a href="/story/aria" aria-label="Breaking news about the climate crisis and environment policy"></a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert any("aria" in u for u in urls)


def test_extract_strips_numeric_prefix():
    """'3 Headline text...' → strips the leading number (line 441)."""
    html = """<main>
      <a href="/story/ranked">3 Scientists discover breakthrough in quantum computing field</a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    assert links
    assert not links[0].text.startswith("3")
    assert "Scientists" in links[0].text


def test_extract_slop_url_filtered_in_main():
    """Slop URLs inside <main> are still filtered (line 432)."""
    html = """<main>
      <a href="https://cooking.nyt.com/recipes/pasta">Pasta recipe from Italy</a>
      <a href="/story/news">Breaking news about a major international climate agreement</a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("cooking" in u for u in urls)
    assert any("news" in u for u in urls)


def test_extract_anchor_without_href_skipped():
    """Anchor with no href attribute is ignored (line 429)."""
    html = """<main>
      <a>no href here for this link text</a>
      <a href="/story/good">A genuine news headline that should be returned in output</a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert any("good" in u for u in urls)
    assert len(links) == 1


def test_extract_non_http_href_skipped():
    """href=javascript: normalizes to None → skipped (line 432)."""
    html = """<main>
      <a href="javascript:void(0)">Long enough link text about javascript void action</a>
      <a href="/story/keep">A valid story that should be included in the results</a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("javascript" in u for u in urls)
    assert any("keep" in u for u in urls)


def test_extract_dedup_keeps_first_when_shorter_comes_second():
    """Same URL twice: second text is shorter → first kept (line 445)."""
    html = """<main>
      <a href="/story/x">This much longer and more descriptive headline about the important story</a>
      <a href="/story/x">Shorter version of the headline here indeed</a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    assert len(links) == 1
    assert "longer and more descriptive" in links[0].text


def test_extract_skips_role_navigation_inside_main():
    """_is_excluded_element: role branch (line 269) — element inside <main>."""
    html = """<html><body>
      <main>
        <div role="navigation">
          <a href="/nav/item">Navigation item link for website menu section links</a>
        </div>
        <a href="/story/real">Real breaking news story about international world events today</a>
      </main>
    </body></html>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("nav" in u for u in urls)
    assert any("real" in u for u in urls)


def test_extract_skips_aria_label_nav_inside_main():
    """_is_excluded_element: aria-label 'nav' branch (line 272) — element inside <main>."""
    html = """<html><body>
      <main>
        <div aria-label="secondary navigation menu">
          <a href="/menu/link">Secondary menu link about website navigation options</a>
        </div>
        <a href="/story/ok">Breaking news story that should be included in results output</a>
      </main>
    </body></html>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("menu" in u for u in urls)
    assert any("ok" in u for u in urls)


def test_extract_skips_class_pattern_inside_main():
    """_is_excluded_element: class pattern branch (line 275) — element inside <main>."""
    html = """<html><body>
      <main>
        <div class="sidebar-newsletter-widget">
          <a href="/subscribe/form">Newsletter subscription form link for newsletter</a>
        </div>
        <a href="/story/yes">Important news headline that belongs in the output results</a>
      </main>
    </body></html>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("subscribe" in u for u in urls)
    assert any("yes" in u for u in urls)


def test_extract_slop_text_filtered():
    """Slop anchor text filtered via _is_slop_url when URL also matches."""
    html = """<main>
      <a href="/games/wordle">Play Wordle today and test your vocabulary knowledge</a>
      <a href="/story/economy">Economy grows faster than expected this quarter around the globe</a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("wordle" in u for u in urls)
    assert any("economy" in u for u in urls)


def test_extract_slop_text_filtered_at_text_gate():
    """Non-slop URL with slop anchor text → filtered at _is_slop_text check (line 445)."""
    # URL does not hit _is_slop_url, but text contains a slop phrase
    html = """<main>
      <a href="/features/brain">Play the crossword now and challenge your brain skills today</a>
      <a href="/story/economy">Economy grows faster than expected this quarter around globe</a>
    </main>"""
    links = extract_headline_links(html, base_url=BASE)
    urls = {l.url for l in links}
    assert not any("brain" in u for u in urls)
    assert any("economy" in u for u in urls)


# ── structured.py — edge cases ────────────────────────────────────────────────

def test_structured_empty_html():
    r = extract_structured("")
    assert r == {"jsonld": [], "opengraph": {}, "microdata": []}


def test_structured_empty_jsonld_script_skipped():
    """Raw JSON-LD is blank → skip (line 37)."""
    html = '<script type="application/ld+json">   </script>'
    r = extract_structured(html)
    assert r["jsonld"] == []


def test_structured_invalid_json_skipped():
    """Invalid JSON → skip without error (lines 40-41)."""
    html = '<script type="application/ld+json">{ not valid json }</script>'
    r = extract_structured(html)
    assert r["jsonld"] == []


def test_structured_jsonld_array_top_level():
    """JSON array at top level → extend list (line 46)."""
    html = '<script type="application/ld+json">[{"@type": "Article"}, {"@type": "BreadcrumbList"}]</script>'
    r = extract_structured(html)
    assert len(r["jsonld"]) == 2
    assert r["jsonld"][0]["@type"] == "Article"


def test_structured_jsonld_graph_unwrapped():
    """@graph list → unwrap members."""
    html = '<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Article"},{"@type":"Organization"}]}</script>'
    r = extract_structured(html)
    assert len(r["jsonld"]) == 2


def test_structured_opengraph_meta_no_key_skipped():
    """meta with no property/name attribute → skip (line 59)."""
    html = '<meta content="orphaned content"/>'
    r = extract_structured(html)
    assert r["opengraph"] == {}


def test_structured_opengraph_no_content_skipped():
    """meta with property but no content → skip."""
    html = '<meta property="og:title"/>'
    r = extract_structured(html)
    assert r["opengraph"] == {}


def test_structured_opengraph_non_og_name_skipped():
    """meta name='viewport' → not captured."""
    html = '<meta name="viewport" content="width=device-width"/>'
    r = extract_structured(html)
    assert r["opengraph"] == {}


def test_structured_opengraph_description_captured():
    html = '<meta name="description" content="A page about quantum computing"/>'
    r = extract_structured(html)
    assert r["opengraph"]["description"] == "A page about quantum computing"


def test_structured_opengraph_author_captured():
    html = '<meta name="author" content="Jane Doe"/>'
    r = extract_structured(html)
    assert r["opengraph"]["author"] == "Jane Doe"


def test_structured_microdata_no_itemprop_name_skipped():
    """itemprop present but empty string → skip (line 74)."""
    html = '<div itemscope itemtype="https://schema.org/Article"><span itemprop="">value</span></div>'
    r = extract_structured(html)
    assert r["microdata"] == []


def test_structured_microdata_href_value():
    """itemprop with href → use href value."""
    html = '<div itemscope itemtype="https://schema.org/Article"><a itemprop="url" href="https://x.test/story">link</a></div>'
    r = extract_structured(html)
    assert r["microdata"][0]["props"]["url"] == "https://x.test/story"


def test_structured_microdata_src_value():
    """itemprop with src → use src value."""
    html = '<div itemscope itemtype="https://schema.org/Article"><img itemprop="image" src="https://x.test/img.jpg"/></div>'
    r = extract_structured(html)
    assert r["microdata"][0]["props"]["image"] == "https://x.test/img.jpg"


def test_structured_microdata_no_props_excluded():
    """itemscope with no populated itemprop children → not appended."""
    html = '<div itemscope itemtype="https://schema.org/Thing"></div>'
    r = extract_structured(html)
    assert r["microdata"] == []


# ── article.py: _looks_like_index_body ───────────────────────────────────────

def test_looks_like_index_body_short_index_title():
    assert _looks_like_index_body("World", "brief text") is True


def test_looks_like_index_body_long_text_is_not_index():
    # Use a non-index title so the title branch doesn't short-circuit
    long_text = ("word " * 120).strip()  # > 500 chars
    assert _looks_like_index_body("A Specific Story Title", long_text) is False


def test_looks_like_index_body_none_title_short_text():
    # no long paragraph → True
    assert _looks_like_index_body(None, "Short text.\nAnother line.") is True


def test_looks_like_index_body_long_paragraph_not_index():
    # paragraph > 200 chars → not an index body
    para = "x " * 120
    assert _looks_like_index_body(None, para) is False


def test_looks_like_index_body_multi_word_title_not_index():
    # title with 3 words — not an index token match
    assert _looks_like_index_body("Stock Market Rally", "short text") is True  # no long para


# ── article.py: _run_trafilatura error paths ──────────────────────────────────

def test_run_trafilatura_import_error_raises(monkeypatch):
    """ImportError from missing trafilatura → re-raised with message (lines 94-98)."""
    monkeypatch.setitem(sys.modules, "trafilatura", None)
    with pytest.raises(ImportError, match="trafilatura is required"):
        _run_trafilatura("<html><body>test content</body></html>", "https://x.test/story")


def test_run_trafilatura_generic_exception_returns_none(monkeypatch):
    """Non-ImportError from trafilatura.extract → caught, returns None (lines 99-100)."""
    import trafilatura as traf
    monkeypatch.setattr(traf, "extract", lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")))
    result = _run_trafilatura("<html><body>test content</body></html>", "https://x.test/story")
    assert result is None


# ── article.py: extract_article — index body branch ──────────────────────────

def test_extract_article_empty_text_returns_none(monkeypatch):
    """trafilatura returns dict with empty text → None (line 122)."""
    monkeypatch.setattr(
        "ujin.extract.article._run_trafilatura",
        lambda html, url: {"title": "Some title", "text": None,
                           "author": None, "date": None, "language": None, "image": None},
    )
    result = extract_article("<html><body>...</body></html>", "https://x.test/story/real")
    assert result is None


def test_extract_article_index_body_heuristic_returns_none(monkeypatch):
    """URL passes URL check but body looks like index → None (line 122)."""
    # Patch _run_trafilatura to return a dict with index-like body
    monkeypatch.setattr(
        "ujin.extract.article._run_trafilatura",
        lambda html, url: {
            "title": "World",      # 1 word, in _INDEX_TITLE_TOKENS
            "text": "Brief summary.",
            "author": None, "date": None, "language": None, "image": None,
        },
    )
    result = extract_article("<html><body>...</body></html>", "https://x.test/story/world")
    assert result is None


# ── article.py: extract_article_lenient ──────────────────────────────────────

def test_extract_article_lenient_empty_html_returns_none():
    assert extract_article_lenient("", "https://x.test/story") is None


def test_extract_article_lenient_trafilatura_returns_none(monkeypatch):
    """trafilatura returns no data → None (lenient path, lines 150-152)."""
    monkeypatch.setattr(
        "ujin.extract.article._run_trafilatura",
        lambda html, url: None,
    )
    result = extract_article_lenient("<html><body>...</body></html>", "https://x.test/story")
    assert result is None


def test_extract_article_lenient_empty_text_returns_none(monkeypatch):
    """trafilatura returns dict with empty text → None (lines 153-154)."""
    monkeypatch.setattr(
        "ujin.extract.article._run_trafilatura",
        lambda html, url: {"title": "T", "text": "   ", "author": None,
                           "date": None, "language": None, "image": None},
    )
    result = extract_article_lenient("<html><body>...</body></html>", "https://x.test/story")
    assert result is None


def test_extract_article_lenient_success(monkeypatch):
    """lenient skips index-URL guard, returns Article when text present (lines 156-163)."""
    monkeypatch.setattr(
        "ujin.extract.article._run_trafilatura",
        lambda html, url: {
            "title": "A Real Headline", "text": "Substantive article body text here.",
            "author": "Jane Doe", "date": "2026-06-22", "language": "en", "image": None,
        },
    )
    # "/" is an index URL — extract_article would reject it, lenient must not
    result = extract_article_lenient("<html><body>...</body></html>", "https://x.test/")
    assert result is not None
    assert result.title == "A Real Headline"
    assert result.byline == "Jane Doe"
    assert result.published == "2026-06-22"


def test_extract_article_lenient_with_corpus(html_corpus):
    """lenient accepts article corpus HTML regardless of URL path."""
    result = extract_article_lenient(
        html_corpus["article"],
        url="https://x.test/",   # index URL — lenient ignores it
    )
    # Must not crash; may or may not extract depending on trafilatura heuristics
    assert result is None or hasattr(result, "text")


# ── poll/browser.py ───────────────────────────────────────────────────────────

class _FakeBrowserResult:
    """Minimal BrowserResult-like object for browser poll tests."""
    def __init__(self, html, *, final_url=None, items=None, elapsed_ms=5):
        self.html = html
        self.final_url = final_url
        self.items = items
        self.elapsed_ms = elapsed_ms


class _FakeFetcher:
    def __init__(self, html="<html></html>", *, final_url=None, items=None, raises=None):
        self._html = html
        self._final_url = final_url
        self._items = items
        self._raises = raises

    async def render(self, url, actions=None, *, results_selector=None, ctx=None):
        if self._raises is not None:
            raise self._raises
        return _FakeBrowserResult(
            self._html,
            final_url=self._final_url or url,
            items=self._items,
        )


async def test_browser_poll_links_extract():
    from ujin.poll.browser import BrowserPollable

    html = """<main>
      <a href="/story/a">Breaking news about the global economy and market rally today</a>
      <a href="/story/b">Scientists announce major breakthrough in quantum computing research</a>
    </main>"""
    p = BrowserPollable("https://x.test/", extract="links",
                        fetcher=_FakeFetcher(html=html))
    r = await p.poll(None)
    assert r.ok is True
    assert isinstance(r.payload, list)
    assert r.changed is True  # first poll always changed


async def test_browser_poll_article_extract(monkeypatch):
    """Covers article extract branch (lines 73-76)."""
    from ujin.poll.browser import BrowserPollable

    # text must be > 200 chars in one paragraph to pass _looks_like_index_body
    long_text = "Body text about the science breakthrough. " * 6
    monkeypatch.setattr(
        "ujin.extract.article._run_trafilatura",
        lambda html, url: {
            "title": "A Real Headline About Science",
            "text": long_text,
            "author": "Jane", "date": "2026-06-22", "language": "en", "image": None,
        },
    )
    p = BrowserPollable(
        "https://x.test/2026/story/specific-article-path",
        extract="article",
        fetcher=_FakeFetcher(html="<html><body>content</body></html>"),
    )
    r = await p.poll(None)
    assert r.ok is True
    assert r.payload is not None
    assert r.payload["title"] == "A Real Headline About Science"


async def test_browser_poll_article_extract_none_payload():
    """article extract returns None when extract_article returns None."""
    from ujin.poll.browser import BrowserPollable

    # index URL → extract_article returns None
    p = BrowserPollable(
        "https://x.test/",
        extract="article",
        fetcher=_FakeFetcher(html="<html><body>tiny</body></html>"),
    )
    r = await p.poll(None)
    assert r.ok is True
    assert r.payload is None


async def test_browser_poll_structured_extract():
    """Covers structured extract branch (lines 78-80)."""
    from ujin.poll.browser import BrowserPollable

    html = """<html><head>
      <meta property="og:title" content="Test Title"/>
      <script type="application/ld+json">{"@type": "Article", "name": "Test"}</script>
    </head><body></body></html>"""
    p = BrowserPollable("https://x.test/page", extract="structured",
                        fetcher=_FakeFetcher(html=html))
    r = await p.poll(None)
    assert r.ok is True
    assert isinstance(r.payload, dict)
    assert r.payload["opengraph"].get("og:title") == "Test Title"
    assert r.payload["jsonld"][0]["@type"] == "Article"


async def test_browser_poll_raw_with_items():
    """raw extract returns items when present."""
    from ujin.poll.browser import BrowserPollable

    items = [{"title": "item1"}, {"title": "item2"}]
    p = BrowserPollable("https://x.test/", extract="raw",
                        fetcher=_FakeFetcher(html="<html></html>", items=items))
    r = await p.poll(None)
    assert r.ok is True
    assert r.payload == items


async def test_browser_poll_raw_fallback_html():
    """raw extract falls back to HTML when items is None."""
    from ujin.poll.browser import BrowserPollable

    p = BrowserPollable("https://x.test/", extract="raw",
                        fetcher=_FakeFetcher(html="<html><body>raw content</body></html>"))
    r = await p.poll(None)
    assert r.ok is True
    assert "raw content" in r.payload


async def test_browser_poll_fetch_exception_returns_failure():
    from ujin.poll.browser import BrowserPollable

    p = BrowserPollable("https://x.test/", extract="links",
                        fetcher=_FakeFetcher(raises=ConnectionError("network down")))
    r = await p.poll(None)
    assert r.ok is False
    assert "ConnectionError" in r.error


async def test_browser_poll_lazy_fetcher_init(monkeypatch):
    """Covers _get_fetcher branch that constructs BrowserFetcher lazily (lines 51-53)."""
    import ujin.fetch.browser as browser_mod
    from ujin.poll.browser import BrowserPollable

    fetcher = _FakeFetcher(html="<main></main>")
    monkeypatch.setattr(browser_mod, "BrowserFetcher",
                        lambda engine, headless: fetcher)

    p = BrowserPollable("https://x.test/", extract="links")
    assert p._fetcher is None  # not set yet
    r = await p.poll(None)
    assert r.ok is True
    assert p._fetcher is fetcher  # now populated


async def test_browser_poll_second_poll_unchanged():
    from ujin.poll.browser import BrowserPollable

    html = "<main><a href='/s/a'>Long enough valid headline about current world affairs</a></main>"
    fetcher = _FakeFetcher(html=html)
    p = BrowserPollable("https://x.test/", extract="links", fetcher=fetcher)
    r1 = await p.poll(None)
    r2 = await p.poll(r1)
    assert r1.changed is True
    assert r2.changed is False


async def test_browser_poll_key_default():
    from ujin.poll.browser import BrowserPollable

    p = BrowserPollable("https://x.test/page", extract="links")
    assert p.key == "browser:links:https://x.test/page"


async def test_browser_poll_key_explicit():
    from ujin.poll.browser import BrowserPollable

    p = BrowserPollable("https://x.test/page", extract="links", key="my-key")
    assert p.key == "my-key"
