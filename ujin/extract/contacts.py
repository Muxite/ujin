"""Contact information extraction from HTML pages.

    extract_contacts(
        '<html><body>'
        '<a href="mailto:alice@example.com">Email us</a>'
        '<a href="tel:+15551234567">Call us</a>'
        '<a href="https://twitter.com/example">Twitter</a>'
        '</body></html>',
        base_url="https://example.com/",
    )
    # -> {
    #     "emails": ["alice@example.com"],
    #     "phones": ["+15551234567"],
    #     "links": ["https://twitter.com/example"],
    # }

Emails are collected from ``mailto:`` hrefs and plain-text patterns in
visible page text.  Phone numbers come from ``tel:`` hrefs and common
numeric patterns in visible text (international ``+…`` format and NANP
``(NXX) NXX-XXXX``).  Social / profile links are collected from ``<a>``
elements whose ``href`` resolves to a well-known social platform or whose
``rel`` attribute contains ``"me"``.

All three lists are de-duplicated in document order (first occurrence wins).
Relative hrefs in social links are resolved against ``base_url`` via
:func:`urllib.parse.urljoin` when supplied.

Pure stdlib (:mod:`html.parser`, :mod:`re`).  Returns ``{}`` — never raises —
for empty or malformed input.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

# Inline e-mail pattern — deliberately conservative to avoid false positives.
_EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)

# Phone patterns: international (+…) or North-American (NXX) NXX-XXXX.
_PHONE_RE = re.compile(
    r'(?:'
    r'\+\d[\d\s\-().]{6,}\d'               # +1 (555) 123-4567
    r'|'
    r'\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}'  # (555) 123-4567 / 555.123.4567
    r')'
)

_SOCIAL_DOMAINS = frozenset({
    "twitter.com", "x.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "github.com", "gitlab.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
    "reddit.com",
    "snapchat.com",
    "tumblr.com",
    "medium.com",
    "t.me",
    "wa.me",
    "discord.com", "discord.gg",
    "twitch.tv",
    "vimeo.com",
    "flickr.com",
    "behance.net",
    "dribbble.com",
    "stackoverflow.com",
})


def _is_social_href(href: str, rel: str) -> bool:
    """True when *href* resolves to a social platform or *rel* contains ``me``."""
    if "me" in rel.lower().split():
        return True
    try:
        host = urlparse(href).netloc.lower()
        host_no_www = host[4:] if host.startswith("www.") else host
        return host in _SOCIAL_DOMAINS or host_no_www in _SOCIAL_DOMAINS
    except Exception:  # noqa: BLE001
        return False


def _resolve(value: str, base_url: Optional[str]) -> str:
    """Make *value* absolute against *base_url* (unchanged when no base given)."""
    if base_url:
        try:
            return urljoin(base_url, value)
        except Exception:  # noqa: BLE001
            return value
    return value


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def extract_contacts(html: str, base_url: Optional[str] = None) -> dict:
    """Extract contact information from *html*.

    Returns a dict with ``"emails"``, ``"phones"``, and ``"links"`` lists,
    each de-duplicated in document order.  Returns ``{}`` — never raises —
    for empty or malformed input.
    """
    if not html or not html.strip():
        return {}

    parser = _ContactsParser(base_url=base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — parser failure must not propagate
        pass

    return {
        "emails": _dedup(parser.emails),
        "phones": _dedup(parser.phones),
        "links": _dedup(parser.links),
    }


class _ContactsParser(HTMLParser):
    """Walk the document collecting emails, phones, and social/profile links."""

    def __init__(self, base_url: Optional[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._skip = False  # inside <script> or <style>
        self.emails: list[str] = []
        self.phones: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip = True
            return
        if self._skip or tag != "a":
            return
        a = dict(attrs)
        href = (a.get("href") or "").strip()
        if not href:
            return
        lower = href.lower()
        if lower.startswith("mailto:"):
            # Strip query-string parameters like ?subject=Hello
            email = href[7:].split("?")[0].strip()
            if email:
                self.emails.append(email)
        elif lower.startswith("tel:"):
            phone = href[4:].strip()
            if phone:
                self.phones.append(phone)
        else:
            rel = (a.get("rel") or "").lower()
            if _is_social_href(href, rel):
                self.links.append(_resolve(href, self._base_url))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        for m in _EMAIL_RE.finditer(data):
            self.emails.append(m.group())
        for m in _PHONE_RE.finditer(data):
            self.phones.append(m.group().strip())
