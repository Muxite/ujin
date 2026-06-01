"""Chained X (Twitter) post fetcher.

Order: nitter (free, RSS) → syndication (free, JSON) → brave (paid, search).
First leg that returns a non-empty list wins. Per-leg failures are silent.

The Brave leg respects a caller-provided budget so we don't burn credits
when the free legs are healthy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from ._nitter import NitterPool, nitter_posts
from ._syndication import syndication_posts
from .twitter import (
    BraveError,
    BraveNotConfigured,
    SocialPost,
    twitter_search,
)

logger = logging.getLogger("ujin.sources.social.x")


@dataclass
class ChainResult:
    posts: list[SocialPost]
    leg: str  # "nitter" | "syndication" | "brave" | "empty"


# A simple sync predicate; the budget object lives in the caller (seulgi),
# but we accept a callable so the scraper stays state-light here.
BudgetGate = Callable[[], bool]


async def x_posts(
    username: str,
    count: int = 20,
    *,
    nitter: Optional[NitterPool] = None,
    allow_brave: bool = True,
    brave_gate: Optional[BudgetGate] = None,
) -> ChainResult:
    """Walk the free → paid chain. Returns first non-empty hit."""
    username = username.lstrip("@").strip()
    if not username:
        return ChainResult(posts=[], leg="empty")

    if nitter is not None and nitter.mirrors:
        try:
            posts = await nitter_posts(nitter, username, count)
        except Exception as exc:  # noqa: BLE001
            logger.debug("nitter chain leg crashed for %s: %s", username, exc)
            posts = []
        if posts:
            return ChainResult(posts=posts, leg="nitter")

    try:
        posts = await syndication_posts(username, count)
    except Exception as exc:  # noqa: BLE001
        logger.debug("syndication chain leg crashed for %s: %s", username, exc)
        posts = []
    if posts:
        return ChainResult(posts=posts, leg="syndication")

    if allow_brave and (brave_gate is None or brave_gate()):
        try:
            posts = await twitter_search(username, count)
        except BraveNotConfigured:
            return ChainResult(posts=[], leg="empty")
        except BraveError as exc:
            logger.debug("brave leg failed for %s: %s", username, exc)
            return ChainResult(posts=[], leg="empty")
        if posts:
            return ChainResult(posts=posts, leg="brave")

    return ChainResult(posts=[], leg="empty")
