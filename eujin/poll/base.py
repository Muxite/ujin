"""Poll contracts: what it means to be pollable, and the result shape.

A *pollable* is anything eujin can check repeatedly for change — an HTTP page, an
RSS feed, a JSON API, a shell command, or an arbitrary Python callable. Each poll
returns a :class:`PollResult` carrying a content ``fingerprint`` so the engine can
tell whether anything changed and adapt its cadence accordingly.

This module is dependency-free; concrete roles import their optional deps lazily.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


def fingerprint(data: Any) -> str:
    """Stable sha256 of arbitrary data (bytes/str/JSON-able) for change detection.

    Mirrors the fingerprint idea in ``eujin.cache.store.CachedEntry`` but works on
    any payload: bytes as-is, str utf-8, everything else via canonical JSON.
    """
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif isinstance(data, str):
        raw = data.encode("utf-8", "replace")
    else:
        raw = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass
class PollResult:
    """Outcome of a single poll.

    ``changed`` is set by the caller (engine) by comparing ``fingerprint`` to the
    previous result; a pollable may also set it directly when it has a cheaper
    change signal (e.g. HTTP 304).
    """

    ok: bool = True
    changed: bool = False
    fingerprint: str | None = None
    payload: Any = None
    latency_ms: int = 0
    status: int | None = None
    error: str | None = None
    retry_after: float | None = None  # provider-supplied wait (seconds)
    ts: float = field(default_factory=time.time)

    @classmethod
    def failure(cls, error: str, *, retry_after: float | None = None,
                status: int | None = None) -> "PollResult":
        return cls(ok=False, error=error, retry_after=retry_after, status=status)


@runtime_checkable
class Pollable(Protocol):
    """Anything eujin can poll.

    ``key`` is a stable identity used for state/persistence. ``poll`` receives the
    previous :class:`PollResult` (or ``None`` on first poll) so it can do
    conditional fetches (ETag/Last-Modified) and decide ``changed``.
    """

    key: str

    async def poll(self, prev: "PollResult | None") -> PollResult: ...


def decide_changed(new_fp: str | None, prev: PollResult | None) -> bool:
    """True when the new fingerprint differs from the previous one.

    First successful poll counts as changed (there was nothing before).
    """
    if new_fp is None:
        return False
    if prev is None or prev.fingerprint is None:
        return True
    return new_fp != prev.fingerprint
