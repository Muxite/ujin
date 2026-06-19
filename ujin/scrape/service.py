"""High-level orchestration: combine fetch + cache + extract + fallbacks.

This is the only module the routes talk to. Routes shouldn't know
whether a fetch hit obscura, ETag-revalidated, fell through to a
sitemap-news, or came from cache — they just see a `ScrapeResult`.

Ported from jennie/scraper-v2 with two decouplings:
  * configuration is injected as a :class:`ujin.scrape.config.ScrapeConfig`
    rather than a module-level ``settings`` singleton;
  * link tiering / breaking-score / poll-hint policy is delegated to a
    pluggable :class:`ujin.scrape.scoring.Scorer` (default :class:`NullScorer`),
    so the orchestrator carries no news-trading semantics.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional

from ..cache import CachedEntry, HostPolicy, ScrapeCache
from ..extract import (
    NormalizedLink,
    apply_article_profile,
    apply_link_profile,
    extract_article,
    extract_headline_links,
)

if TYPE_CHECKING:
    from ..extract import Article
from ..extract.links import fingerprint_links
from ..fetch import HttpFetcher, ObscuraFetcher
from ..poll.base import fingerprint as _fingerprint
from ..fetch.altpath import AltPathResult, try_rss_fallback, try_sitemap_news
from ..sources.rss import parse_feed
from .config import ScrapeConfig
from .host_overrides import HostOverrideRegistry
from .metrics import HostMetrics
from .scoring import NullScorer, Scorer

logger = logging.getLogger("ujin.scrape.service")


Mode = Literal["links", "article", "auto", "combined", "structured"]


@dataclass
class ScrapeResult:
    url: str
    kind: str  # "links" | "article" | "structured" | "empty"
    fingerprint: str
    fetched_at: float
    cached: bool
    age_secs: float
    used_renderer: bool
    strategy_used: str = "http"  # http|obscura|sitemap_news|rss|cache
    links: list[NormalizedLink] = field(default_factory=list)
    article: Optional["Article"] = None
    structured: Optional[dict] = None
    final_url: Optional[str] = None
    note: Optional[str] = None
    next_poll_hint_secs: Optional[float] = None


class HostCooldown(RuntimeError):
    """Host is in cooldown — caller should retry later."""


class ScrapeService:
    def __init__(
        self,
        http: HttpFetcher,
        obscura: ObscuraFetcher,
        cache: ScrapeCache,
        policy: HostPolicy,
        *,
        config: Optional[ScrapeConfig] = None,
        metrics: Optional[HostMetrics] = None,
        overrides: Optional[HostOverrideRegistry] = None,
        scorer: Optional[Scorer] = None,
        browser: Any = None,
    ):
        self._http = http
        self._obscura = obscura
        self._cache = cache
        self._policy = policy
        self._config = config or ScrapeConfig()
        self._metrics = metrics or HostMetrics()
        self._overrides = overrides or HostOverrideRegistry()
        self._scorer = scorer or NullScorer()
        self._browser = browser  # optional BrowserFetcher for render="browser"

    async def scrape(
        self,
        url: str,
        *,
        mode: Mode = "links",
        force_refresh: bool = False,
        enrich_html_top_n: int = 0,
        render: str = "auto",
        actions: Optional[list[dict]] = None,
    ) -> ScrapeResult:
        loop_start = time.monotonic()

        # `combined` is a parallel RSS+HTML fan-out on top of the existing
        # primitives. Dispatch early so the rest of the function stays simple.
        if mode == "combined":
            return await self._scrape_combined(
                url,
                force_refresh=force_refresh,
                enrich_html_top_n=enrich_html_top_n,
                loop_start=loop_start,
            )

        override = self._overrides.lookup(url)

        remaining = self._policy.cooldown_remaining(url)
        if remaining > 0 and not force_refresh:
            cached = self._cache.get(f"{mode}:{url}")
            if cached is not None:
                result = self._result_from_cache(
                    cached, mode, note=f"host cooldown {remaining:.0f}s; served cache"
                )
                self._metrics.record(
                    url,
                    success=True,
                    latency_ms=(time.monotonic() - loop_start) * 1000,
                    cached=True,
                    strategy="cache",
                )
                return result
            self._metrics.record(
                url,
                success=False,
                latency_ms=(time.monotonic() - loop_start) * 1000,
                strategy="cooldown",
            )
            raise HostCooldown(f"{url} on cooldown for {remaining:.0f}s")

        cache_key = f"{mode}:{url}"
        cached = None if force_refresh else self._cache.get(cache_key)

        # Override paths short-circuit the normal chain.
        if override.strategy == "sitemap_news" and mode != "article":
            alt = await self._direct_sitemap(url, override.sitemap_url)
            if alt is not None:
                return self._finalize_links(
                    url, mode, alt.links, "sitemap_news", loop_start, cache_key
                )
        if override.strategy == "rss" and mode != "article":
            alt = await self._direct_rss(override.rss_url)
            if alt is not None:
                return self._finalize_links(
                    url, mode, alt.links, "rss", loop_start, cache_key
                )

        # An explicit `render=` pins the strategy (overriding the per-host
        # override); "auto" keeps the per-host/default escalation.
        effective_strategy = render if render != "auto" else override.strategy

        html, used_renderer, http_meta, final_url, not_modified, fetch_strategy, prelinks = (
            await self._fetch_html(
                url,
                mode=mode,
                cached=cached,
                force_refresh=force_refresh,
                override_strategy=effective_strategy,
                actions=actions,
            )
        )

        if not_modified and cached is not None:
            result = self._result_from_cache(cached, mode, note="304 Not Modified")
            self._metrics.record(
                url, success=True,
                latency_ms=(time.monotonic() - loop_start) * 1000,
                cached=True, strategy="http_304",
            )
            return result

        # Try the altpath chain when primary fetch produced nothing OR
        # produced too few links to be useful (and we're in links mode).
        synth_links: Optional[list[NormalizedLink]] = None
        synth_strategy: Optional[str] = None
        # Extract links at most once per scrape and reuse the result for both
        # the thin-result altpath decision and the final link-set build. The
        # HTTP fast-path already extracted them (``prelinks``); otherwise we
        # extract here from whatever body `_fetch_html` returned.
        extracted_links: Optional[list[NormalizedLink]] = None
        if mode in ("links", "auto"):
            if html is None:
                should_try_alt = True
            elif mode == "links":
                extracted_links = (
                    prelinks
                    if prelinks is not None
                    else self._extract_with_profile(html, base_url=final_url or url)
                )
                should_try_alt = len(extracted_links) < self._config.fast_path_min_links
            else:
                should_try_alt = False
            if should_try_alt:
                alt = await self._walk_altpath_chain(url, override)
                if alt is not None:
                    synth_links = alt.links
                    synth_strategy = alt.strategy

        if html is None and synth_links is None:
            self._policy.record_failure(url)
            self._metrics.record(
                url, success=False,
                latency_ms=(time.monotonic() - loop_start) * 1000,
                used_renderer=used_renderer,
            )
            raise RuntimeError(f"fetch failed for {url}")

        self._policy.record_success(url)

        # If altpath won, use its links and skip extraction.
        if synth_links is not None and (html is None or mode != "article"):
            return self._finalize_links(
                url, mode, synth_links, synth_strategy or "altpath",
                loop_start, cache_key, used_renderer=used_renderer,
            )

        if mode == "structured":
            from ..extract.structured import extract_structured

            structured = extract_structured(html)
            fingerprint = _fingerprint(structured)
            entry = CachedEntry(
                url=url,
                fingerprint=fingerprint,
                payload={"structured": structured},
                fetched_at=time.monotonic(),
                etag=http_meta.get("etag"),
                last_modified=http_meta.get("last_modified"),
            )
            self._cache.put(cache_key, entry)
            self._metrics.record(
                url, success=True,
                latency_ms=(time.monotonic() - loop_start) * 1000,
                used_renderer=used_renderer, strategy=fetch_strategy,
            )
            return ScrapeResult(
                url=url, kind="structured", fingerprint=fingerprint,
                fetched_at=time.time(), cached=False, age_secs=0.0,
                used_renderer=used_renderer, strategy_used=fetch_strategy,
                structured=structured, final_url=final_url,
            )

        if mode == "article":
            override = self._overrides.lookup(url)
            article = None
            if override.extract.has_article_profile:
                article = apply_article_profile(
                    html, final_url or url, override.extract.article
                )
            if article is None:
                article = extract_article(html, url=final_url or url)
            if article is None:
                fingerprint = ""
                kind = "empty"
            else:
                fingerprint = hashlib.sha256(
                    article.text.encode("utf-8")
                ).hexdigest()
                kind = "article"

            entry = CachedEntry(
                url=url,
                fingerprint=fingerprint,
                payload={"article": article},
                fetched_at=time.monotonic(),
                etag=http_meta.get("etag"),
                last_modified=http_meta.get("last_modified"),
            )
            self._cache.put(cache_key, entry)

            self._metrics.record(
                url, success=(article is not None),
                latency_ms=(time.monotonic() - loop_start) * 1000,
                used_renderer=used_renderer, strategy=fetch_strategy,
            )
            return ScrapeResult(
                url=url, kind=kind, fingerprint=fingerprint,
                fetched_at=time.time(), cached=False, age_secs=0.0,
                used_renderer=used_renderer, strategy_used=fetch_strategy,
                article=article, final_url=final_url,
            )

        # links / auto mode — reuse the single extraction done above (links
        # mode) or extract once here (auto mode never extracted yet).
        links = (
            extracted_links
            if extracted_links is not None
            else self._extract_with_profile(html, base_url=final_url or url)
        )
        return self._finalize_links(
            url, mode, links, fetch_strategy, loop_start, cache_key,
            http_meta=http_meta, final_url=final_url,
            used_renderer=used_renderer,
        )

    # ── combined RSS+HTML strategy ─────────────────────────────────────────

    async def _scrape_combined(
        self,
        url: str,
        *,
        force_refresh: bool,
        enrich_html_top_n: int,
        loop_start: float,
    ) -> ScrapeResult:
        """Fetch RSS and HTML for a homepage in parallel and merge link sets.

        Resolution order for the RSS feed URL:
          1. Per-host override `rss_url`.
          2. `<link rel="alternate">` discovery from the HTML fetch we'll
             already be doing.
        If neither is found, the combined response degenerates to plain
        HTML extraction.
        """
        import asyncio

        from ..extract.links import (
            NormalizedLink,
            _is_boilerplate_text,
            _is_slop_url,
            _strip_numeric_prefix,
            fingerprint_links,
            normalize_url,
        )
        from ..sources.rss import parse_feed

        override = self._overrides.lookup(url)

        # 1. Fetch HTML in parallel with (optional) pinned-RSS feed.
        html_task = asyncio.create_task(
            self.scrape(url, mode="links", force_refresh=force_refresh)
        )
        rss_task: Optional[asyncio.Task] = None
        if override.rss_url:
            rss_task = asyncio.create_task(parse_feed(override.rss_url))

        html_result: Optional[ScrapeResult] = None
        html_err: Optional[Exception] = None
        try:
            html_result = await html_task
        except Exception as exc:  # noqa: BLE001
            html_err = exc

        rss_items: list = []
        rss_err: Optional[Exception] = None
        if rss_task is not None:
            try:
                rss_items = await rss_task
            except Exception as exc:  # noqa: BLE001
                rss_err = exc

        # 2. Merge by canonical URL. RSS wins on metadata (summary,
        # published); we union the seen_in tuple.
        merged: dict[str, NormalizedLink] = {}

        # Apply the same boilerplate / slop-URL filters to RSS items that the
        # HTML extractor uses, so feeds with short video/teaser items don't
        # bypass the headline gate.
        for item in rss_items:
            canon = normalize_url(item.url, base=url) or item.url
            if _is_slop_url(canon):
                continue
            raw_title = (item.title or "").strip()
            cleaned_title = _strip_numeric_prefix(raw_title)
            if _is_boilerplate_text(cleaned_title):
                continue
            existing = merged.get(canon)
            seen = ("rss",) if existing is None else tuple(set(existing.seen_in + ("rss",)))
            merged[canon] = NormalizedLink(
                url=canon,
                text=cleaned_title or (existing.text if existing else ""),
                summary=(item.summary or "").strip(),
                published=item.published or "",
                seen_in=seen,
            )

        if html_result is not None and html_result.kind == "links":
            for link in html_result.links:
                canon = link.url
                existing = merged.get(canon)
                if existing is None:
                    merged[canon] = NormalizedLink(
                        url=canon,
                        text=link.text,
                        summary="",
                        published="",
                        seen_in=("html",),
                    )
                else:
                    merged[canon] = NormalizedLink(
                        url=canon,
                        text=existing.text or link.text,
                        summary=existing.summary,
                        published=existing.published,
                        seen_in=tuple(set(existing.seen_in + ("html",))),
                    )

        # 3. Optional article-body fan-out for HTML-only links.
        if enrich_html_top_n > 0:
            await self._enrich_html_only_links(merged, top_n=enrich_html_top_n)

        links = list(merged.values())
        self._scorer.score_links(links, base_url=url)
        note = (
            f"rss:{len(rss_items)} + "
            f"html:{len(html_result.links) if html_result is not None else 0} → "
            f"dedup:{len(links)}"
        )
        if rss_err is not None:
            note += f"; rss_err={type(rss_err).__name__}"
        if html_err is not None:
            note += f"; html_err={type(html_err).__name__}"

        fingerprint = fingerprint_links(links)
        self._metrics.record(
            url, success=bool(links),
            latency_ms=(time.monotonic() - loop_start) * 1000,
            strategy="combined",
        )

        return ScrapeResult(
            url=url,
            kind="links" if links else "empty",
            fingerprint=fingerprint,
            fetched_at=time.time(),
            cached=False,
            age_secs=0.0,
            used_renderer=html_result.used_renderer if html_result else False,
            strategy_used="combined",
            links=links,
            final_url=html_result.final_url if html_result else None,
            note=note,
        )

    async def _enrich_html_only_links(
        self,
        merged: "dict[str, object]",
        *,
        top_n: int,
    ) -> None:
        """For up to `top_n` links that have an HTML hit but no RSS summary,
        run `mode=article` in parallel and attach the first paragraph as
        their summary. Failures are silent — the link keeps its empty
        summary."""
        import asyncio

        from ..extract.links import NormalizedLink

        html_only = [
            (canon, link) for canon, link in merged.items()
            if "html" in link.seen_in and "rss" not in link.seen_in
            and not link.summary
        ][:top_n]
        if not html_only:
            return

        async def _enrich(canon_url, link):
            try:
                art_result = await self.scrape(link.url, mode="article")
            except Exception:  # noqa: BLE001
                return canon_url, None
            if art_result.article is None or not art_result.article.text:
                return canon_url, None
            # First non-empty paragraph as summary; cap at 600 chars.
            for para in art_result.article.text.split("\n\n"):
                p = para.strip()
                if len(p) >= 60:
                    return canon_url, p[:600]
            return canon_url, None

        results = await asyncio.gather(
            *(_enrich(c, l) for c, l in html_only),
            return_exceptions=False,
        )
        for canon_url, summary in results:
            if not summary:
                continue
            existing = merged[canon_url]
            merged[canon_url] = NormalizedLink(
                url=existing.url,
                text=existing.text,
                summary=summary,
                published=existing.published,
                seen_in=tuple(set(existing.seen_in + ("article",))),
            )

    # ── batch ──────────────────────────────────────────────────────────────

    async def scrape_batch(
        self, requests: list[tuple[str, Mode, bool]]
    ) -> list[object]:
        """Fan out N scrape calls concurrently.

        Each item: (url, mode, force_refresh). Return values match the
        order of requests; failed items appear as the exception object
        the caller can render to an error response."""
        import asyncio

        async def _one(item):
            url, mode, force = item
            try:
                return await self.scrape(url, mode=mode, force_refresh=force)
            except Exception as exc:  # noqa: BLE001
                return exc

        return await asyncio.gather(*(_one(r) for r in requests))

    # ── helpers ────────────────────────────────────────────────────────────

    def _extract_with_profile(
        self, html: str, *, base_url: str
    ) -> list[NormalizedLink]:
        """Per-site profile first; fall back to generic extractor.

        When a profile exists but produces fewer than 5 links we run the
        generic extractor and union — guards against a too-restrictive
        selector list silently zero'ing out a redesigned site.
        """
        override = self._overrides.lookup(base_url)
        profile = override.extract
        if profile.has_link_profile:
            profile_links = apply_link_profile(html, base_url, profile)
            if len(profile_links) >= 5:
                return profile_links
            generic = extract_headline_links(html, base_url=base_url)
            seen = {l.url for l in profile_links}
            for g in generic:
                if g.url not in seen:
                    profile_links.append(g)
                    seen.add(g.url)
            return profile_links
        return extract_headline_links(html, base_url=base_url)

    def _finalize_links(
        self,
        url: str,
        mode: Mode,
        links: list[NormalizedLink],
        strategy: str,
        loop_start: float,
        cache_key: str,
        *,
        http_meta: Optional[dict] = None,
        final_url: Optional[str] = None,
        used_renderer: bool = False,
    ) -> ScrapeResult:
        http_meta = http_meta or {}
        # Universal topic filter — RSS, sitemap, html all funnel through here.
        # apply_link_profile already filters HTML links, but RSS/sitemap don't.
        # A re-filter is a no-op for HTML (already clean) and the only filter
        # for the other paths.
        links = self._filter_named_links(final_url or url, links)
        self._scorer.score_links(links, base_url=final_url or url)
        fingerprint = fingerprint_links(links)

        prior = self._cache.get(cache_key)
        prior_unchanged = prior is not None and prior.fingerprint == fingerprint
        hint = self._scorer.poll_hint(
            links, base_url=url, prior_unchanged=prior_unchanged
        )

        if prior_unchanged:
            refreshed = CachedEntry(
                url=url,
                fingerprint=fingerprint,
                payload=prior.payload,
                fetched_at=time.monotonic(),
                etag=http_meta.get("etag") or prior.etag,
                last_modified=http_meta.get("last_modified") or prior.last_modified,
                hits=prior.hits,
            )
            self._cache.put(cache_key, refreshed)
            self._metrics.record(
                url, success=True,
                latency_ms=(time.monotonic() - loop_start) * 1000,
                used_renderer=used_renderer, cached=True, strategy=strategy,
            )
            return ScrapeResult(
                url=url, kind="links", fingerprint=fingerprint,
                fetched_at=time.time(), cached=True, age_secs=0.0,
                used_renderer=used_renderer, strategy_used=strategy,
                links=prior.payload.get("links", []),
                final_url=final_url, note="content unchanged",
                next_poll_hint_secs=hint,
            )

        entry = CachedEntry(
            url=url,
            fingerprint=fingerprint,
            payload={"links": links},
            fetched_at=time.monotonic(),
            etag=http_meta.get("etag"),
            last_modified=http_meta.get("last_modified"),
        )
        self._cache.put(cache_key, entry)
        self._metrics.record(
            url, success=bool(links),
            latency_ms=(time.monotonic() - loop_start) * 1000,
            used_renderer=used_renderer, strategy=strategy,
        )
        return ScrapeResult(
            url=url, kind="links", fingerprint=fingerprint,
            fetched_at=time.time(), cached=False, age_secs=0.0,
            used_renderer=used_renderer, strategy_used=strategy,
            links=links, final_url=final_url,
            next_poll_hint_secs=hint,
        )

    async def _fetch_html(
        self,
        url: str,
        *,
        mode: Mode,
        cached: Optional[CachedEntry],
        force_refresh: bool,
        override_strategy: str = "auto",
        actions: Optional[list[dict]] = None,
    ) -> tuple[Optional[str], bool, dict, Optional[str], bool, str, Optional[list[NormalizedLink]]]:
        """Return (html, used_renderer, http_meta, final_url, not_modified, strategy, prelinks).

        ``prelinks`` carries the links already extracted from ``html`` when this
        method had to run the extractor itself (the links-mode HTTP fast-path),
        so the caller can reuse them instead of re-parsing the same body.
        It is ``None`` whenever ``html`` was not produced by that path (obscura
        render, browser snapshot, non-links mode), since those bodies are either
        different from what was extracted or were never extracted here.
        """
        etag = cached.etag if cached and not force_refresh else None
        last_modified = (
            cached.last_modified if cached and not force_refresh else None
        )

        http_meta: dict = {}
        final_url: Optional[str] = None
        strategy = "http"

        # Browser strategy: run the interaction recipe and snapshot HTML. Pinned
        # via render="browser"; never an automatic fallback (it's expensive).
        if override_strategy == "browser":
            if self._browser is None:
                logger.warning("render='browser' but no browser fetcher wired for %s", url)
                return None, True, http_meta, final_url or url, False, "browser", None
            try:
                r = await self._browser.render(url, actions or [])
                return r.html, True, http_meta, r.final_url or url, False, "browser", None
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser render failed for %s: %s", url, exc)
                return None, True, http_meta, final_url or url, False, "browser", None

        thin_body: Optional[str] = None
        prelinks: Optional[list[NormalizedLink]] = None
        if override_strategy not in ("obscura",):
            try:
                resp = await self._http.get(
                    url, etag=etag, last_modified=last_modified
                )
                final_url = resp.final_url or url
                http_meta = {
                    "etag": resp.etag,
                    "last_modified": resp.last_modified,
                    "status": resp.status,
                }
                if resp.not_modified:
                    return None, False, http_meta, final_url, True, "http_304", None
                if resp.status == 200 and resp.body:
                    if mode != "links":
                        return resp.body, False, http_meta, final_url, False, "http", None
                    prelinks = self._extract_with_profile(
                        resp.body, base_url=final_url or url
                    )
                    if len(prelinks) >= self._config.fast_path_min_links:
                        return resp.body, False, http_meta, final_url, False, "http", prelinks
                    thin_body = resp.body
                    logger.debug(
                        "HTTP fast-path: only %d links for %s; trying obscura",
                        len(prelinks), url,
                    )
                elif 400 <= resp.status < 600:
                    logger.info(
                        "HTTP %s for %s; trying obscura", resp.status, url
                    )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "HTTP fetch error for %s: %s; trying obscura", url, exc
                )

        if override_strategy == "http":
            # User pinned this host to HTTP only; don't escalate — but a thin
            # 200 is still an answer, not a failure (0.4.0 fix: previously
            # the body was discarded and the scrape failed outright).
            # `prelinks` matches `thin_body` (both come from resp.body) when we
            # extracted; it's None when the fetch never produced a 200 body.
            return thin_body, False, http_meta, final_url, False, "http", prelinks

        try:
            result = await self._obscura.render_html(url)
            return result.html, True, http_meta, final_url or url, False, "obscura", None
        except Exception as exc:  # noqa: BLE001
            logger.warning("obscura render failed for %s: %s", url, exc)
            return None, True, http_meta, final_url, False, "obscura", None

    async def _walk_altpath_chain(
        self, url: str, override
    ) -> Optional[AltPathResult]:
        """Try sitemap-news → discovered-RSS in sequence."""
        try:
            alt = await try_sitemap_news(self._http, url)
            if alt is not None:
                return alt
        except Exception as exc:  # noqa: BLE001
            logger.debug("sitemap_news altpath errored for %s: %s", url, exc)

        # Use override RSS if present, otherwise skip — we don't eagerly
        # discover here because that's a second round-trip.
        if override.rss_url:
            try:
                alt = await try_rss_fallback(override.rss_url, parse_feed)
                if alt is not None:
                    return alt
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "rss altpath errored for %s (rss=%s): %s",
                    url, override.rss_url, exc,
                )
        return None

    async def _direct_sitemap(
        self, url: str, pinned: Optional[str]
    ) -> Optional[AltPathResult]:
        """Override-driven direct sitemap fetch.

        Sitemap entries bypass apply_link_profile, so we apply the
        host's `url_path_deny_patterns` + `title_deny_patterns` here
        manually. Otherwise wires would dump their entire sitemap
        (including sports, lotteries, recipes) into the link-set.
        """
        if pinned:
            try:
                resp = await self._http.get(pinned)
                if resp.status == 200 and resp.body:
                    from ..sources.sitemap import parse_sitemap_xml

                    entries = parse_sitemap_xml(resp.body)
                    links = self._filter_sitemap_entries(url, entries)
                    if links:
                        return AltPathResult(strategy="sitemap_news", links=links)
            except Exception:  # noqa: BLE001
                pass
        # Fallback to auto-discovery; still filter through deny patterns.
        result = await try_sitemap_news(self._http, url)
        if result is None:
            return None
        filtered = self._filter_named_links(url, result.links)
        return AltPathResult(strategy=result.strategy, links=filtered) if filtered else result

    def _filter_sitemap_entries(self, base_url: str, entries) -> list[NormalizedLink]:
        import re as _re
        from urllib.parse import urlsplit as _urlsplit

        from ..extract.links import normalize_url

        override = self._overrides.lookup(base_url)
        profile = override.extract
        path_deny = [
            _re.compile(p, _re.IGNORECASE)
            for p in (profile.url_path_deny_patterns or ())
        ]
        title_deny = [
            _re.compile(p, _re.IGNORECASE)
            for p in (profile.title_deny_patterns or ())
        ]
        path_must = None
        if profile.url_path_must_match:
            try:
                path_must = _re.compile(profile.url_path_must_match)
            except _re.error:
                path_must = None

        links: list[NormalizedLink] = []
        seen: set[str] = set()
        for e in entries[:600]:
            canon = normalize_url(e.url, base=base_url)
            if not canon or canon in seen:
                continue
            path = _urlsplit(canon).path
            if path_must is not None and not path_must.search(path):
                continue
            if any(r.search(path) for r in path_deny):
                continue
            title = (e.title or "").strip()
            if title and any(r.search(title) for r in title_deny):
                continue
            seen.add(canon)
            links.append(NormalizedLink(url=canon, text=title))
            if len(links) >= 300:
                break
        return links

    def _filter_named_links(self, base_url: str, links: list[NormalizedLink]) -> list[NormalizedLink]:
        import re as _re
        from urllib.parse import urlsplit as _urlsplit

        override = self._overrides.lookup(base_url)
        profile = override.extract
        path_deny = [
            _re.compile(p, _re.IGNORECASE)
            for p in (profile.url_path_deny_patterns or ())
        ]
        title_deny = [
            _re.compile(p, _re.IGNORECASE)
            for p in (profile.title_deny_patterns or ())
        ]
        out: list[NormalizedLink] = []
        for link in links:
            path = _urlsplit(link.url).path
            if any(r.search(path) for r in path_deny):
                continue
            if link.text and any(r.search(link.text) for r in title_deny):
                continue
            out.append(link)
        return out

    async def _direct_rss(
        self, pinned: Optional[str]
    ) -> Optional[AltPathResult]:
        return await try_rss_fallback(pinned, parse_feed)

    def _result_from_cache(
        self, entry: CachedEntry, mode: Mode, *, note: Optional[str] = None
    ) -> ScrapeResult:
        if mode == "article":
            return ScrapeResult(
                url=entry.url,
                kind="article" if entry.payload.get("article") else "empty",
                fingerprint=entry.fingerprint,
                fetched_at=time.time(),
                cached=True, age_secs=entry.age_secs,
                used_renderer=False, strategy_used="cache",
                article=entry.payload.get("article"),
                note=note,
            )
        if mode == "structured":
            return ScrapeResult(
                url=entry.url,
                kind="structured" if entry.payload.get("structured") else "empty",
                fingerprint=entry.fingerprint,
                fetched_at=time.time(),
                cached=True, age_secs=entry.age_secs,
                used_renderer=False, strategy_used="cache",
                structured=entry.payload.get("structured"),
                note=note,
            )
        return ScrapeResult(
            url=entry.url, kind="links",
            fingerprint=entry.fingerprint,
            fetched_at=time.time(),
            cached=True, age_secs=entry.age_secs,
            used_renderer=False, strategy_used="cache",
            links=entry.payload.get("links", []),
            note=note,
        )
