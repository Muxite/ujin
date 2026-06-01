"""BreakingScorer — the opt-in news-trading :class:`Scorer`.

Encapsulates everything the generic orchestrator deliberately omits: per-link
source tiering, breaking-score composition, cross-source corroboration ingest,
and a breaking-aware poll hint. Wire it into ``create_scrape_app(scorer=...)``
(or ``ScrapeService(scorer=...)``) to reproduce jennie/scraper-v2's behavior.
"""
from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlsplit

from ..scrape.host_overrides import HostOverrideRegistry
from .corroboration import CorroborationStore
from .tier import Weights, breaking_score, tier_of

# Generic hint constants (match scraper-v2 _finalize_links).
_BASE_HINT = 60.0
_MAX_HINT = 300.0


class BreakingScorer:
    """News-trading scorer: tier + breaking_score + corroboration + poll hint."""

    def __init__(
        self,
        *,
        overrides: Optional[HostOverrideRegistry] = None,
        corroboration: Optional[CorroborationStore] = None,
        trend_terms_provider: Optional[Callable[[], list]] = None,
        weights: Optional[Weights] = None,
        breaking_threshold: float = 0.40,
    ) -> None:
        self._overrides = overrides or HostOverrideRegistry()
        self._corroboration = corroboration
        self._trend_terms_provider = trend_terms_provider
        self._weights = weights or Weights()
        self._breaking_threshold = breaking_threshold

    def score_links(self, links: list, *, base_url: str) -> None:
        """Annotate each link with `_tier` / `_breaking_score` / `_score_components`."""
        override = self._overrides.lookup(base_url)
        host_tier = override.tier or tier_of(urlsplit(base_url).netloc, None)
        terms: list = []
        if self._trend_terms_provider is not None:
            try:
                terms = list(self._trend_terms_provider())
            except Exception:
                terms = []

        for link in links:
            host = urlsplit(link.url).netloc.lower() or urlsplit(base_url).netloc.lower()
            link_override = (
                self._overrides.lookup_host(host)
                if hasattr(self._overrides, "lookup_host")
                else override
            )
            link_tier = (link_override.tier if link_override else host_tier) or host_tier
            score, components = breaking_score(
                url=link.url,
                title=link.text,
                tier_label=link_tier,
                published_iso=getattr(link, "published", None) or None,
                corroboration=self._corroboration,
                trend_terms=terms,
                weights=self._weights,
            )
            object.__setattr__(link, "_tier", link_tier)
            object.__setattr__(link, "_breaking_score", round(float(score), 4))
            object.__setattr__(
                link,
                "_score_components",
                {k: round(float(v), 4) for k, v in components.items()},
            )
            if self._corroboration is not None and link.text:
                try:
                    self._corroboration.add(link.text, host)
                except Exception:
                    pass

    def poll_hint(
        self, links: list, *, base_url: str, prior_unchanged: bool
    ) -> float | None:
        hint = _BASE_HINT
        override = self._overrides.lookup(base_url)
        if (override.tier or "").lower() == "wire":
            hint = 30.0
        max_score = max(
            (getattr(l, "_breaking_score", 0.0) for l in links), default=0.0
        )
        if max_score >= self._breaking_threshold:
            hint = min(hint, 20.0)
        if prior_unchanged:
            hint = min(_MAX_HINT, hint * 2)
        return hint
