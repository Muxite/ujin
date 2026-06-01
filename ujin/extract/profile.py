"""Per-site link + article extractors driven by ExtractProfile.

Falls back to the generic extractor when no profile is configured.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser, Node

from ..scrape.host_overrides import ArticleProfile, ExtractProfile
from .article import Article
from .links import (
    NormalizedLink,
    _is_boilerplate_text,
    _is_slop_url,
    _strip_numeric_prefix,
    normalize_url,
)

logger = logging.getLogger("ujin.extract.profile")


def _strip_affixes(
    title: str, prefixes: tuple[str, ...], suffixes: tuple[str, ...]
) -> str:
    t = title.strip()
    for p in prefixes:
        if p and t.startswith(p):
            t = t[len(p):].strip()
    for s in suffixes:
        if s and t.endswith(s):
            t = t[: -len(s)].strip()
    return t


def apply_link_profile(
    html: str,
    base_url: str,
    profile: ExtractProfile,
    *,
    max_links: int = 200,
) -> list[NormalizedLink]:
    """Run a per-site link extraction. Returns headline links."""
    if not html or not profile.has_link_profile:
        return []
    tree = HTMLParser(html)
    if tree.body is None:
        return []

    path_re: Optional[re.Pattern] = None
    if profile.url_path_must_match:
        try:
            path_re = re.compile(profile.url_path_must_match)
        except re.error as exc:
            logger.warning("invalid url_path_must_match on profile: %s", exc)
            path_re = None

    path_deny_res: list[re.Pattern] = []
    for pat in profile.url_path_deny_patterns:
        try:
            path_deny_res.append(re.compile(pat, re.IGNORECASE))
        except re.error as exc:
            logger.warning("invalid url_path_deny_patterns entry %r: %s", pat, exc)

    title_deny_res: list[re.Pattern] = []
    for pat in profile.title_deny_patterns:
        try:
            title_deny_res.append(re.compile(pat, re.IGNORECASE))
        except re.error as exc:
            logger.warning("invalid title_deny_patterns entry %r: %s", pat, exc)

    exclude_nodes: set[int] = set()
    for sel in profile.link_excludes:
        for n in tree.css(sel):
            exclude_nodes.add(id(n))

    min_chars = profile.min_title_chars or 30
    seen: dict[str, NormalizedLink] = {}

    selectors = profile.link_selectors or ("a",)
    for sel in selectors:
        for a in tree.css(sel):
            if id(a) in exclude_nodes:
                continue
            href = a.attributes.get("href")
            if not href:
                continue
            canon = normalize_url(href, base=base_url)
            if not canon:
                continue
            if _is_slop_url(canon):
                continue
            path = urlsplit(canon).path
            if path_re is not None and not path_re.search(path):
                continue
            if any(r.search(path) for r in path_deny_res):
                continue
            raw_text = (a.text() or "").strip()
            cleaned = _strip_numeric_prefix(raw_text)
            cleaned = _strip_affixes(
                cleaned,
                profile.title_strip_prefixes,
                profile.title_strip_suffixes,
            )
            if len(cleaned) < min_chars:
                continue
            if _is_boilerplate_text(cleaned):
                continue
            if any(r.search(cleaned) for r in title_deny_res):
                continue
            existing = seen.get(canon)
            if existing is None or len(cleaned) > len(existing.text or ""):
                seen[canon] = NormalizedLink(url=canon, text=cleaned)
            if len(seen) >= max_links:
                break
        if len(seen) >= max_links:
            break
    return list(seen.values())


def apply_article_profile(
    html: str, url: str, profile: ArticleProfile
) -> Optional[Article]:
    if not html or profile is None:
        return None
    tree = HTMLParser(html)

    title = _select_text(tree, profile.title)
    body_node: Optional[Node] = None
    if profile.body:
        body_node = tree.css_first(profile.body)
    if body_node is None:
        return None
    paras: list[str] = []
    for n in body_node.css("p"):
        t = (n.text() or "").strip()
        if t and len(t) >= 30:
            paras.append(t)
    text = "\n\n".join(paras).strip()
    if not text:
        return None

    byline = _select_text(tree, profile.byline)
    published = None
    if profile.published_meta:
        meta = tree.css_first(f"meta[property='{profile.published_meta}']")
        if meta is None:
            meta = tree.css_first(f"meta[name='{profile.published_meta}']")
        if meta is not None:
            published = (meta.attributes.get("content") or "").strip() or None

    return Article(
        url=url,
        title=title or "",
        text=text,
        byline=byline,
        published=published,
        language=None,
        top_image=None,
    )


def _select_text(tree: HTMLParser, selector: Optional[str]) -> Optional[str]:
    if not selector:
        return None
    node = tree.css_first(selector)
    if node is None:
        return None
    text = (node.text() or "").strip()
    return text or None
