"""Structured-data extraction: JSON-LD, OpenGraph/meta, and microdata.

Returns a single dict::

    {
      "jsonld": [ {...}, ... ],     # parsed <script type=application/ld+json>
      "opengraph": { "og:title": ..., "twitter:card": ..., "description": ... },
      "microdata": [ { "type": ..., "props": {...} }, ... ],
    }

This is robust change-detection fuel (schema.org Article/Product/Event payloads
change meaningfully even when page chrome churns) and clean LLM input. Uses
selectolax (``web``/``diff`` extra).
"""
from __future__ import annotations

import json
from typing import Any


def extract_structured(html: str) -> dict[str, Any]:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html or "")
    return {
        "jsonld": _jsonld(tree),
        "opengraph": _opengraph(tree),
        "microdata": _microdata(tree),
    }


def _jsonld(tree) -> list[Any]:
    out: list[Any] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = (node.text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        # A single document may hold a @graph list or a top-level array.
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            out.extend(data["@graph"])
        elif isinstance(data, list):
            out.extend(data)
        else:
            out.append(data)
    return out


def _opengraph(tree) -> dict[str, str]:
    out: dict[str, str] = {}
    for meta in tree.css("meta"):
        attrs = meta.attributes
        key = attrs.get("property") or attrs.get("name")
        content = attrs.get("content")
        if not key or content is None:
            continue
        if key.startswith(("og:", "twitter:", "article:")) or key in (
            "description", "author", "keywords",
        ):
            out[key] = content
    return out


def _microdata(tree) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for scope in tree.css("[itemscope]"):
        item: dict[str, Any] = {"type": scope.attributes.get("itemtype"), "props": {}}
        for prop in scope.css("[itemprop]"):
            name = prop.attributes.get("itemprop")
            if not name:
                continue
            value = (
                prop.attributes.get("content")
                or prop.attributes.get("href")
                or prop.attributes.get("src")
                or (prop.text() or "").strip()
            )
            if value:
                item["props"][name] = value
        if item["props"]:
            out.append(item)
    return out
