"""Per-site extraction profiles: apply_link_profile / apply_article_profile."""
from __future__ import annotations

from ujin.extract.profile import (
    _select_text,
    _strip_affixes,
    apply_article_profile,
    apply_link_profile,
)
from ujin.scrape.host_overrides import ArticleProfile, ExtractProfile

BASE = "https://site.example.com/"

HTML = """<html><body>
<div class="rail">
  <a href="/promo/subscribe-now-and-save">Subscribe now and save big today!</a>
</div>
<main>
  <a class="story" href="/2026/06/09/alpha">BREAKING: Alpha story headline long enough</a>
  <a class="story" href="/2026/06/09/beta">12. Beta story headline that is long enough</a>
  <a class="story" href="/sports/gamma">Gamma sports story headline long enough</a>
  <a class="story" href="/2026/06/09/short">tiny</a>
  <a class="story" href="/2026/06/09/alpha">BREAKING: Alpha story headline long enough — extended edition</a>
  <a class="story">no href here at all on this anchor element</a>
  <a class="story" href="javascript:void(0)">A javascript pseudo link long enough</a>
</main>
</body></html>"""


def _profile(**kw) -> ExtractProfile:
    defaults = dict(link_selectors=("a.story",))
    defaults.update(kw)
    return ExtractProfile(**defaults)


def test_link_profile_selects_and_normalizes():
    links = apply_link_profile(HTML, BASE, _profile())
    urls = {l.url for l in links}
    assert f"{BASE}2026/06/09/alpha" in urls
    assert f"{BASE}2026/06/09/beta" in urls
    # rail promo not matched by selector; short titles dropped; js dropped
    assert not any("promo" in u for u in urls)
    assert not any(u.endswith("/short") for u in urls)


def test_link_profile_dedupes_keeping_longer_title():
    links = apply_link_profile(HTML, BASE, _profile())
    alpha = next(l for l in links if l.url.endswith("/alpha"))
    assert "extended edition" in alpha.text


def test_link_profile_path_deny():
    links = apply_link_profile(
        HTML, BASE, _profile(url_path_deny_patterns=("/sports/",)))
    assert not any("/sports/" in l.url for l in links)


def test_link_profile_path_must_match():
    links = apply_link_profile(
        HTML, BASE, _profile(url_path_must_match=r"^/2026/"))
    assert links and all("/2026/" in l.url for l in links)


def test_link_profile_invalid_regexes_logged_not_fatal():
    links = apply_link_profile(
        HTML, BASE,
        _profile(url_path_must_match="([unclosed",
                 url_path_deny_patterns=("([also-bad",),
                 title_deny_patterns=("([worse",)))
    assert links  # extraction proceeded with the bad patterns ignored


def test_link_profile_title_deny():
    links = apply_link_profile(
        HTML, BASE, _profile(title_deny_patterns=("^BREAKING",)))
    assert not any(l.text.startswith("BREAKING") for l in links)


def test_link_profile_strip_prefixes():
    links = apply_link_profile(
        HTML, BASE, _profile(title_strip_prefixes=("BREAKING:",)))
    alpha = next(l for l in links if l.url.endswith("/alpha"))
    assert not alpha.text.startswith("BREAKING")
    assert alpha.text.startswith("Alpha story")


def test_link_profile_excludes_selector():
    html = HTML.replace('class="rail"', 'class="rail"')  # rail present
    links = apply_link_profile(
        html, BASE,
        _profile(link_selectors=("a",), link_excludes=(".rail a",),
                 min_title_chars=10))
    assert not any("promo" in l.url for l in links)


def test_link_profile_min_title_chars_raises_bar_only():
    """min_title_chars can tighten the gate, but the generic boilerplate
    filter still rejects very short texts — lowering it can't readmit them."""
    links = apply_link_profile(HTML, BASE, _profile(min_title_chars=2))
    assert not any(l.url.endswith("/short") for l in links)
    # tightening works: a 50-char floor drops the 40-char gamma headline
    tight = apply_link_profile(HTML, BASE, _profile(min_title_chars=50))
    assert not any(l.url.endswith("/gamma") for l in tight)


def test_link_profile_empty_inputs():
    assert apply_link_profile("", BASE, _profile()) == []
    assert apply_link_profile(HTML, BASE, ExtractProfile()) == []  # no profile


def test_link_profile_max_links_cap():
    rows = "".join(
        f'<a class="story" href="/2026/06/09/s{i}">'
        f"A sufficiently long headline number {i}</a>" for i in range(50)
    )
    links = apply_link_profile(f"<html><body>{rows}</body></html>", BASE,
                               _profile(), max_links=10)
    assert len(links) == 10


def test_strip_affixes():
    assert _strip_affixes(" PRE: title | SITE ", ("PRE:",), ("| SITE",)) == "title"
    assert _strip_affixes("title", (), ()) == "title"
    assert _strip_affixes("title", ("",), ("",)) == "title"


# ── article profile ─────────────────────────────────────────────────────────

ART_HTML = """<html><head>
<meta property="article:published_time" content="2026-06-09T10:00:00Z">
</head><body>
<h1 class="headline">The article headline</h1>
<div class="byline-block">By A. Writer</div>
<div class="article-body">
  <p>The first paragraph is comfortably longer than thirty characters.</p>
  <p>short</p>
  <p>The second paragraph also exceeds the thirty character threshold easily.</p>
</div>
</body></html>"""


def test_article_profile_extracts_fields():
    art = apply_article_profile(ART_HTML, BASE, ArticleProfile(
        body=".article-body", title=".headline", byline=".byline-block",
        published_meta="article:published_time",
    ))
    assert art is not None
    assert art.title == "The article headline"
    assert art.byline == "By A. Writer"
    assert art.published == "2026-06-09T10:00:00Z"
    assert "first paragraph" in art.text and "short" not in art.text


def test_article_profile_published_meta_name_fallback():
    html = ART_HTML.replace('property="article:published_time"',
                            'name="article:published_time"')
    art = apply_article_profile(html, BASE, ArticleProfile(
        body=".article-body", published_meta="article:published_time"))
    assert art.published == "2026-06-09T10:00:00Z"


def test_article_profile_missing_body_returns_none():
    art = apply_article_profile(ART_HTML, BASE,
                                ArticleProfile(body=".no-such-node"))
    assert art is None


def test_article_profile_empty_body_returns_none():
    html = '<html><body><div class="article-body"><p>x</p></div></body></html>'
    art = apply_article_profile(html, BASE, ArticleProfile(body=".article-body"))
    assert art is None  # only sub-threshold paragraphs


def test_article_profile_no_html_or_profile():
    assert apply_article_profile("", BASE, ArticleProfile(body="x")) is None
    assert apply_article_profile(ART_HTML, BASE, None) is None


def test_select_text():
    from selectolax.parser import HTMLParser

    tree = HTMLParser("<html><body><h1> hi </h1><p></p></body></html>")
    assert _select_text(tree, "h1") == "hi"
    assert _select_text(tree, "p") is None     # empty text
    assert _select_text(tree, ".nope") is None
    assert _select_text(tree, None) is None
