"""Extract and fingerprint selector-scoped regions of an HTML page.

A "region" is the normalized text content of everything matching one CSS
selector. Fingerprinting per region (with the same stable hash the poll engine
uses) lets a watcher detect change in just the parts of a page that matter.
"""
from __future__ import annotations

import re

from ujin.poll.base import fingerprint

_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def extract_regions(html: str, selectors: list[str]) -> dict[str, str]:
    """Return ``{selector: normalized concatenated text}`` for each selector.

    Selectors that match nothing map to an empty string (so their later
    appearance registers as a change). Requires selectolax (``diff``/``web``
    extra).
    """
    from selectolax.parser import HTMLParser

    if not selectors:
        return {}
    tree = HTMLParser(html or "")
    out: dict[str, str] = {}
    for sel in selectors:
        parts: list[str] = []
        for node in tree.css(sel):
            parts.append(node.text() or "")
        out[sel] = _normalize(" ".join(parts))
    return out


def region_fingerprints(html: str, selectors: list[str]) -> dict[str, str]:
    """Per-selector stable fingerprint of the region's normalized text."""
    return {sel: fingerprint(text) for sel, text in extract_regions(html, selectors).items()}
