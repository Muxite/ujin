"""Optional news-trading scoring: tiering, corroboration, BreakingScorer.

This subpackage is opt-in. The generic scrape service uses
:class:`ujin.scrape.scoring.NullScorer`; wiring :class:`BreakingScorer` here
recovers the breaking-news semantics (source tier + lede markers + recency +
cross-source corroboration + X-trend overlap) that jennie's irene relies on.
Nothing in the generic path imports this module.
"""
from __future__ import annotations

from .corroboration import Cluster, CorroborationStore, jaccard, shingleset
from .scorer import BreakingScorer
from .tier import Weights, breaking_score, tier_of

__all__ = [
    "CorroborationStore",
    "Cluster",
    "shingleset",
    "jaccard",
    "Weights",
    "breaking_score",
    "tier_of",
    "BreakingScorer",
]
