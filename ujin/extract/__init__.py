"""Extraction layer: article bodies, headline links, per-site profiles.

Pulls the ``web`` extra (trafilatura/selectolax).
"""
from __future__ import annotations

from .article import Article, extract_article, extract_article_lenient
from .links import (
    NormalizedLink,
    extract_headline_links,
    fingerprint_links,
    normalize_url,
)
from .profile import apply_article_profile, apply_link_profile

__all__ = [
    "Article",
    "extract_article",
    "extract_article_lenient",
    "NormalizedLink",
    "normalize_url",
    "extract_headline_links",
    "fingerprint_links",
    "apply_link_profile",
    "apply_article_profile",
]
