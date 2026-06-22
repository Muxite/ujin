"""Page-level head metadata → one flat, normalized summary dict.

    extract_metadata(
        '<html lang="en"><head><title>Hi</title>'
        '<link rel="canonical" href="/p"><meta property="og:image" content="/a.jpg">'
        '</head></html>',
        base_url="https://x.test/dir/page",
    )
    # -> {"title": "Hi", "language": "en",
    #     "canonical": "https://x.test/p",
    #     "og": {"image": "https://x.test/a.jpg"}}

A flat convenience summary of the document head: the ``<title>``, the meta
``description``, the canonical URL, the page ``language`` (``<html lang>``), an
optional ``author`` / ``published`` / ``modified`` time and ``favicon``, plus
the OpenGraph (``og:*``) and Twitter-card (``twitter:*``) fields collected under
``og`` / ``twitter`` sub-dicts with the prefix stripped. It deliberately
*complements* :func:`ujin.extract.structured.extract_structured` (JSON-LD /
microdata / the raw prefixed OpenGraph map) rather than duplicating it.

Resolution and robustness:

* ``canonical``, ``favicon`` and any ``og``/``twitter`` value that names a URL
  (``og:image``, ``og:url``, ``twitter:image``, …) are made absolute against
  ``base_url`` via :func:`urllib.parse.urljoin`; with no ``base_url`` they are
  returned unchanged.
* When no ``<title>`` / ``<meta name="description">`` is present the flat
  ``title`` / ``description`` fall back to ``og:title`` / ``og:description``.
* Only keys actually present are included — never an empty string or ``None``.
* Pure stdlib (:mod:`html.parser`). Empty or malformed input yields ``{}``
  rather than raising.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin


def extract_metadata(html: str, base_url: Optional[str] = None) -> dict:
    """Parse the head metadata of ``html`` into one normalized dict.

    See the module docstring for the exact shape and URL-resolution rules. Never
    raises: empty/whitespace input returns ``{}`` and a parser hiccup returns
    whatever was assembled before it (``{}`` in the worst case).
    """
    if not html or not html.strip():
        return {}

    parser = _MetaParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — a parser failure must not propagate
        pass

    out: dict = {}

    title = "".join(parser.title_parts).strip()
    if title:
        out["title"] = title
    if parser.lang:
        out["language"] = parser.lang

    og: dict = {}
    twitter: dict = {}
    for key, raw in parser.metas:
        content = (raw or "").strip()
        if not content:
            continue
        lk = key.lower()
        if lk.startswith("og:"):
            sub = key[3:]
            og.setdefault(sub, _resolve_if_url(sub, content, base_url))
        elif lk.startswith("twitter:"):
            sub = key[8:]
            twitter.setdefault(sub, _resolve_if_url(sub, content, base_url))
        elif lk == "description":
            out.setdefault("description", content)
        elif lk == "author":
            out.setdefault("author", content)
        elif lk == "article:published_time":
            out.setdefault("published", content)
        elif lk == "article:modified_time":
            out.setdefault("modified", content)

    # Flat fallbacks so a caller always finds title/description when OG carries it.
    if "title" not in out and og.get("title"):
        out["title"] = og["title"]
    if "description" not in out and og.get("description"):
        out["description"] = og["description"]

    if parser.canonical:
        out["canonical"] = _resolve(parser.canonical, base_url)
    if parser.favicon:
        out["favicon"] = _resolve(parser.favicon, base_url)

    if og:
        out["og"] = og
    if twitter:
        out["twitter"] = twitter

    return out


class _MetaParser(HTMLParser):
    """Collect ``<title>``, ``<html lang>``, head ``<meta>`` and ``<link>`` only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang: Optional[str] = None
        self.title_parts: list[str] = []
        self.metas: list[tuple[str, Optional[str]]] = []
        self.canonical: Optional[str] = None
        self.favicon: Optional[str] = None
        self._in_title = False
        self._title_done = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            lang = a.get("lang") or a.get("xml:lang")
            if lang and lang.strip() and self.lang is None:
                self.lang = lang.strip()
        elif tag == "title" and not self._title_done:
            self._in_title = True
        elif tag == "meta":
            key = a.get("property") or a.get("name")
            if key and key.strip() and a.get("content") is not None:
                self.metas.append((key.strip(), a.get("content")))
        elif tag == "link":
            self._handle_link(a)

    def _handle_link(self, a: dict) -> None:
        href = a.get("href")
        if not href or not href.strip():
            return
        rels = (a.get("rel") or "").lower().split()
        href = href.strip()
        if "canonical" in rels and self.canonical is None:
            self.canonical = href
        if "icon" in rels and self.favicon is None:
            # Covers rel="icon" and rel="shortcut icon" (apple-touch-icon too).
            self.favicon = href

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True


def _resolve(value: str, base_url: Optional[str]) -> str:
    """Make ``value`` absolute against ``base_url`` (unchanged with no base)."""
    if base_url:
        try:
            return urljoin(base_url, value)
        except Exception:  # noqa: BLE001 — a join failure keeps the raw value
            return value
    return value


def _resolve_if_url(sub: str, value: str, base_url: Optional[str]) -> str:
    """Resolve OG/Twitter values whose sub-key names a URL (image/url/etc.)."""
    s = sub.lower()
    if "image" in s or "url" in s or s in ("audio", "video"):
        return _resolve(value, base_url)
    return value
