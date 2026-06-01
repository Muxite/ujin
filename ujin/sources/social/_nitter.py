"""Nitter mirror pool with per-mirror health tracking.

Nitter mirrors come and go. We keep a small pool and a per-mirror health
score (1.0 fresh, 0.0 disqualified). The pool walker yields the highest-
scoring mirror that has not recently failed; on failure the caller bumps
the score down and we try the next.

A successful fetch resets the score to 1.0. After enough consecutive
failures the mirror enters a cooldown — same shape as `cache/HostPolicy`
but private to this module so we don't pollute global host metrics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from ..rss import FeedItem, parse_feed
from .twitter import SocialPost

logger = logging.getLogger("ujin.sources.social.nitter")


_FAILURES_BEFORE_COOLDOWN = 3
_COOLDOWN_SECS = 300.0


@dataclass
class _MirrorState:
    base: str
    score: float = 1.0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    successes: int = 0
    failures: int = 0
    last_latency_ms: Optional[float] = None


@dataclass
class NitterPool:
    mirrors: list[_MirrorState] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str) -> "NitterPool":
        try:
            data = yaml.safe_load(Path(path).read_text()) or {}
        except FileNotFoundError:
            logger.warning("nitter pool yaml missing at %s; pool empty", path)
            return cls()
        urls = data.get("mirrors") or []
        return cls(mirrors=[_MirrorState(base=u.rstrip("/")) for u in urls])

    @classmethod
    def from_list(cls, urls: list[str]) -> "NitterPool":
        return cls(mirrors=[_MirrorState(base=u.rstrip("/")) for u in urls])

    def healthy(self) -> list[_MirrorState]:
        now = time.monotonic()
        return [m for m in self.mirrors if m.cooldown_until <= now]

    def record_success(self, mirror: _MirrorState, latency_ms: float) -> None:
        mirror.score = 1.0
        mirror.consecutive_failures = 0
        mirror.successes += 1
        mirror.last_latency_ms = latency_ms

    def record_failure(self, mirror: _MirrorState) -> None:
        mirror.consecutive_failures += 1
        mirror.failures += 1
        mirror.score = max(0.0, mirror.score - 0.25)
        if mirror.consecutive_failures >= _FAILURES_BEFORE_COOLDOWN:
            mirror.cooldown_until = time.monotonic() + _COOLDOWN_SECS
            mirror.consecutive_failures = 0
            logger.info(
                "nitter %s cooling down %.0fs", mirror.base, _COOLDOWN_SECS
            )

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "base": m.base,
                "score": m.score,
                "successes": m.successes,
                "failures": m.failures,
                "cooldown_remaining": max(0.0, m.cooldown_until - now),
                "last_latency_ms": m.last_latency_ms,
            }
            for m in self.mirrors
        ]


async def nitter_posts(
    pool: NitterPool, username: str, count: int = 20
) -> list[SocialPost]:
    """Walk the pool; return posts from the first mirror that responds.

    Raises nothing — empty list means every healthy mirror failed.
    """
    username = username.lstrip("@").strip()
    if not username:
        return []
    healthy = pool.healthy()
    if not healthy:
        return []
    for mirror in sorted(healthy, key=lambda m: -m.score):
        feed_url = f"{mirror.base}/{username}/rss"
        t0 = time.monotonic()
        try:
            items: list[FeedItem] = await parse_feed(feed_url, timeout_secs=10)
        except Exception as exc:  # noqa: BLE001
            logger.debug("nitter %s/%s failed: %s", mirror.base, username, exc)
            pool.record_failure(mirror)
            continue
        if not items:
            pool.record_failure(mirror)
            continue
        latency_ms = (time.monotonic() - t0) * 1000.0
        pool.record_success(mirror, latency_ms)
        posts: list[SocialPost] = []
        for it in items[:count]:
            text = (it.title or it.summary or "").strip()
            if not text or not it.url:
                continue
            posts.append(SocialPost(url=it.url, text=text))
        return posts
    return []
