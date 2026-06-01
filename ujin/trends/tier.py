"""Breaking-tier scorer.

Composite score over five inputs:
- source_rank    (static, from per_host.yaml `tier:`)
- lede_marker    (BREAKING:/JUST IN:/DEVELOPING:/URGENT)
- recency        (exp-decay on lastmod / published age)
- corroboration  (n-host Jaccard cluster size)
- trend_overlap  (intersection with current X-trends terms)
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .corroboration import CorroborationStore, shingleset


_LEDE_RE = re.compile(
    r"\b(BREAKING|JUST\s*IN|DEVELOPING|URGENT|EXCLUSIVE|LIVE)[:\.\s]",
    re.IGNORECASE,
)
_ALLCAPS_RUN_RE = re.compile(r"\b[A-Z]{4,}(?:[\s\-:]+[A-Z]{4,}){0,2}")

# tier_label -> source_rank weight component (0..1)
_TIER_RANK = {
    "wire": 1.0,
    "mainstream": 0.6,
    "specialty": 0.4,
    "social": 0.5,
    "trend": 0.5,
}


@dataclass
class Weights:
    source_rank: float = 0.20
    lede_marker: float = 0.10
    recency: float = 0.15
    corroboration: float = 0.45
    trend_overlap: float = 0.10


def tier_of(host: str, tier_label_from_config: Optional[str]) -> str:
    if tier_label_from_config:
        return tier_label_from_config
    # Defaults if config doesn't say.
    if any(host.endswith(suffix) for suffix in (
        "apnews.com", "reuters.com", "afp.com", "bbc.co.uk", "bbc.com",
    )):
        return "wire"
    return "mainstream"


def _host_of(url: str) -> str:
    return (urlsplit(url).netloc or "").lower()


def _lede_score(title: str) -> float:
    if not title:
        return 0.0
    if _LEDE_RE.search(title):
        return 1.0
    # Leading all-caps run >= 12 chars.
    m = _ALLCAPS_RUN_RE.match(title.strip())
    if m and len(m.group(0)) >= 12:
        return 0.6
    return 0.0


def _recency_score(published_iso: Optional[str], now: Optional[float] = None) -> float:
    if not published_iso:
        return 0.0
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_secs = (now if now is not None else time.time()) - dt.timestamp()
    if age_secs < 0:
        return 1.0
    # exp(-age / tau), tau = 10 min.
    return math.exp(-age_secs / 600.0)


def _trend_overlap_score(title: str, trend_terms: Iterable[str]) -> float:
    if not trend_terms:
        return 0.0
    title_low = title.lower()
    hits = sum(1 for t in trend_terms if t and t.lower() in title_low)
    if hits == 0:
        return 0.0
    # 1 hit = 0.5, 2+ = 1.0
    return min(1.0, 0.5 + 0.25 * (hits - 1))


def breaking_score(
    *,
    url: str,
    title: str,
    tier_label: str,
    published_iso: Optional[str] = None,
    corroboration: Optional[CorroborationStore] = None,
    trend_terms: Optional[Iterable[str]] = None,
    weights: Optional[Weights] = None,
) -> tuple[float, dict[str, float]]:
    """Return (score, per-component dict)."""
    w = weights or Weights()
    components: dict[str, float] = {}

    sr = _TIER_RANK.get(tier_label, 0.5)
    components["source_rank"] = sr * w.source_rank

    components["lede_marker"] = _lede_score(title) * w.lede_marker
    components["recency"] = _recency_score(published_iso) * w.recency

    if corroboration is not None:
        host = _host_of(url)
        score, _ = corroboration.lookup_score(title, host)
        components["corroboration"] = score * w.corroboration
    else:
        components["corroboration"] = 0.0

    components["trend_overlap"] = _trend_overlap_score(
        title, trend_terms or ()
    ) * w.trend_overlap

    total = sum(components.values())
    return min(1.0, total), components
