"""Per-host strategy overrides + per-site extractor profiles.

Lets us pin specific hosts to a known-good strategy so we don't waste
an obscura render on a site we already know is 403-locked.

Format (a per_host.yaml the deploy points at via ScrapeConfig.per_host_config_path):

    hosts:
      apnews.com:
        strategy: sitemap_news
        sitemap_url: https://apnews.com/news-sitemap-content.xml
        tier: wire
        extract:                            # optional per-site extractor
          link_selectors:
            - "main a[href*='/article/']"
          link_excludes:
            - "[data-key='related']"
          url_path_must_match: "^/article/.+$"
          title_strip_suffixes: [" | AP News"]
          title_strip_prefixes: ["LIVE:"]
          min_title_chars: 30
          article:
            body:   "div.RichTextStoryBody"
            title:  "h1"
            byline: "div.Page-authors a"
            published_meta: "article:published_time"

Strategies:
  http         — HTTP fast path only, never obscura or altpath
  obscura      — obscura render only, no HTTP attempt
  sitemap_news — fetch sitemap_url (or auto-probe) directly
  rss          — fetch rss_url directly
  auto         — default behavior (HTTP → obscura → altpath chain)

Hostname lookup is suffix-aware: a config entry for `nytimes.com`
also matches `www.nytimes.com` and `markets.nytimes.com`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


logger = logging.getLogger("ujin.scrape.host_overrides")


@dataclass(frozen=True)
class ArticleProfile:
    body: Optional[str] = None
    title: Optional[str] = None
    byline: Optional[str] = None
    published_meta: Optional[str] = None  # meta[property=article:published_time]


@dataclass(frozen=True)
class ExtractProfile:
    link_selectors: tuple[str, ...] = ()
    link_excludes: tuple[str, ...] = ()
    url_path_must_match: Optional[str] = None
    # Topic filters — when set, links matching ANY pattern are dropped
    # before they ever enter the link-set. Cheap defence against
    # sports/lottery/local-trivia leaking into the funnel.
    url_path_deny_patterns: tuple[str, ...] = ()
    title_deny_patterns: tuple[str, ...] = ()
    title_strip_suffixes: tuple[str, ...] = ()
    title_strip_prefixes: tuple[str, ...] = ()
    min_title_chars: int = 0  # 0 = use global default in links.py
    article: Optional[ArticleProfile] = None

    @property
    def has_link_profile(self) -> bool:
        return bool(self.link_selectors) or bool(self.url_path_must_match)

    @property
    def has_article_profile(self) -> bool:
        return self.article is not None and bool(self.article.body or self.article.title)


_EMPTY_PROFILE = ExtractProfile()


@dataclass(frozen=True)
class HostOverride:
    strategy: str = "auto"
    sitemap_url: Optional[str] = None
    rss_url: Optional[str] = None
    tier: str = "mainstream"  # wire | mainstream | specialty | social | trend
    extract: ExtractProfile = field(default_factory=lambda: _EMPTY_PROFILE)


class HostOverrideRegistry:
    def __init__(self, by_host: Optional[dict[str, HostOverride]] = None):
        self._by_host: dict[str, HostOverride] = by_host or {}

    @classmethod
    def from_file(cls, path: str | Path) -> "HostOverrideRegistry":
        p = Path(path)
        if not p.exists():
            logger.info("no per_host.yaml at %s; using empty overrides", p)
            return cls({})
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML missing — cannot load per_host.yaml")
            return cls({})
        with p.open() as fh:
            data = yaml.safe_load(fh) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "HostOverrideRegistry":
        hosts_raw = (data or {}).get("hosts") or {}
        by_host: dict[str, HostOverride] = {}
        for host, cfg in hosts_raw.items():
            if not isinstance(cfg, dict):
                continue
            extract_cfg = cfg.get("extract") or {}
            article_cfg = (extract_cfg or {}).get("article") or {}
            article = ArticleProfile(
                body=article_cfg.get("body"),
                title=article_cfg.get("title"),
                byline=article_cfg.get("byline"),
                published_meta=article_cfg.get("published_meta"),
            ) if article_cfg else None
            profile = ExtractProfile(
                link_selectors=tuple(extract_cfg.get("link_selectors") or ()),
                link_excludes=tuple(extract_cfg.get("link_excludes") or ()),
                url_path_must_match=extract_cfg.get("url_path_must_match"),
                url_path_deny_patterns=tuple(extract_cfg.get("url_path_deny_patterns") or ()),
                title_deny_patterns=tuple(extract_cfg.get("title_deny_patterns") or ()),
                title_strip_suffixes=tuple(extract_cfg.get("title_strip_suffixes") or ()),
                title_strip_prefixes=tuple(extract_cfg.get("title_strip_prefixes") or ()),
                min_title_chars=int(extract_cfg.get("min_title_chars") or 0),
                article=article,
            )
            by_host[host.lower()] = HostOverride(
                strategy=cfg.get("strategy", "auto"),
                sitemap_url=cfg.get("sitemap_url"),
                rss_url=cfg.get("rss_url"),
                tier=cfg.get("tier", "mainstream"),
                extract=profile,
            )
        logger.info("loaded %d host overrides", len(by_host))
        return cls(by_host)

    def lookup(self, url: str) -> HostOverride:
        from urllib.parse import urlsplit

        host = urlsplit(url).netloc.lower()
        if host in self._by_host:
            return self._by_host[host]
        # Suffix match: `www.nytimes.com` matches a `nytimes.com` entry.
        for cfg_host, override in self._by_host.items():
            if host.endswith("." + cfg_host):
                return override
        return HostOverride()

    def lookup_host(self, host: str) -> HostOverride:
        host = host.lower()
        if host in self._by_host:
            return self._by_host[host]
        for cfg_host, override in self._by_host.items():
            if host.endswith("." + cfg_host):
                return override
        return HostOverride()

    def is_empty(self) -> bool:
        return not self._by_host

    def hosts(self) -> list[str]:
        return list(self._by_host.keys())
