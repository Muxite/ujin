"""Pluggable link scoring + poll-hint policy for the scrape service.

The scrape orchestrator stays generic: it never imports the news-trading
tiering/corroboration machinery. Instead it delegates two decisions to a
:class:`Scorer`:

* ``score_links`` — annotate each link with ``_tier`` / ``_breaking_score`` /
  ``_score_components`` (read back by the route layer into the wire response).
* ``poll_hint`` — suggest how long the caller should wait before re-polling.

:class:`NullScorer` is the dependency-free default: neutral tier, zero score,
and a generic churn-based hint (doubles when content is unchanged, capped at
300s). jennie wires :class:`ujin.trends.BreakingScorer` instead to recover the
breaking-news semantics — same wire shape, opt-in behavior.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Scorer(Protocol):
    """Strategy for ranking links and pacing polls.

    Implementations mutate ``NormalizedLink`` objects in place (the dataclass
    is frozen, so use ``object.__setattr__``) and return a poll-hint in
    seconds (or ``None`` for "no opinion").
    """

    def score_links(self, links: list, *, base_url: str) -> None: ...

    def poll_hint(
        self, links: list, *, base_url: str, prior_unchanged: bool
    ) -> float | None: ...


# Generic hint constants (mirror scraper-v2's non-trading defaults).
_BASE_HINT = 60.0
_MAX_HINT = 300.0


class NullScorer:
    """Default scorer: neutral metadata + generic churn-based poll hint.

    No news-trading semantics, no optional deps. Sets ``tier="generic"`` and a
    zero breaking score so the wire response shape is identical to the scored
    path while staying honest about the absence of scoring.
    """

    def score_links(self, links: list, *, base_url: str) -> None:
        for link in links:
            object.__setattr__(link, "_tier", "generic")
            object.__setattr__(link, "_breaking_score", 0.0)
            object.__setattr__(link, "_score_components", {})

    def poll_hint(
        self, links: list, *, base_url: str, prior_unchanged: bool
    ) -> float | None:
        hint = _BASE_HINT
        if prior_unchanged:
            hint = min(_MAX_HINT, hint * 2)
        return hint
