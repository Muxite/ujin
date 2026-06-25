"""Declared feed discovery from ``<link rel="alternate">`` head elements.

    extract_feeds(
        '<html><head>'
        '<link rel="alternate" type="application/rss+xml" title="My Feed" href="/feed.xml">'
        '<link rel="alternate" type="application/atom+xml" href="https://x.test/atom.xml">'
        '</head></html>',
        base_url="https://x.test/",
    )
    # -> [
    #     {"href": "https://x.test/feed.xml", "type": "application/rss+xml", "title": "My Feed"},
    #     {"href": "https://x.test/atom.xml",  "type": "application/atom+xml"},
    # ]

Recognized feed MIME types: ``application/rss+xml``, ``application/atom+xml``,
``application/feed+json``.  The type comparison is case-insensitive; the stored
``type`` value is normalized to lowercase.  Relative ``href`` values are made
absolute via :func:`urllib.parse.urljoin` when ``base_url`` is supplied.
Identical ``href`` values (post-resolution) are de-duplicated in document order.

Pure stdlib (:mod:`html.parser`). Empty or malformed input returns ``[]``
rather than raising.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin

_FEED_TYPES = frozenset({
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
})


def extract_feeds(html: str, base_url: Optional[str] = None) -> list[dict]:
    """Discover declared feeds from ``<link rel="alternate">`` head elements.

    Returns a list of dicts, each carrying ``href`` (absolute URL), ``type``
    (lowercase MIME type), and an optional ``title`` (only when non-blank).
    Identical ``href`` values are de-duplicated in document order.  Returns
    ``[]`` — never raises — for empty or malformed input.
    """
    if not html or not html.strip():
        return []

    parser = _FeedsParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — parser failure must not propagate
        pass

    seen: set[str] = set()
    out: list[dict] = []
    for raw_href, feed_type, title in parser.feeds:
        href = _resolve(raw_href, base_url)
        if href in seen:
            continue
        seen.add(href)
        entry: dict = {"href": href, "type": feed_type}
        if title:
            entry["title"] = title
        out.append(entry)
    return out


class _FeedsParser(HTMLParser):
    """Collect ``<link rel="alternate">`` feed links from the document head."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.feeds: list[tuple[str, str, str]] = []  # (href, type, title)
        self._past_head = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "body":
            self._past_head = True
            return
        if self._past_head or tag != "link":
            return
        a = dict(attrs)
        href = (a.get("href") or "").strip()
        if not href:
            return
        rel = (a.get("rel") or "").lower().split()
        if "alternate" not in rel:
            return
        feed_type = (a.get("type") or "").strip().lower()
        if feed_type not in _FEED_TYPES:
            return
        title = (a.get("title") or "").strip()
        self.feeds.append((href, feed_type, title))

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._past_head = True


def _resolve(value: str, base_url: Optional[str]) -> str:
    """Make ``value`` absolute against ``base_url`` (unchanged with no base)."""
    if base_url:
        try:
            return urljoin(base_url, value)
        except Exception:  # noqa: BLE001 — join failure keeps the raw value
            return value
    return value
