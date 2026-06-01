"""Cookie / session persistence for the fetch layer.

A :class:`SessionStore` wraps an aiohttp ``CookieJar`` that survives across
runs (pickled to disk), so login/consent cookies set on one poll are reused on
the next. Hand it to ``HttpFetcher.start(session=store)``.
"""
from __future__ import annotations

from .store import SessionStore

__all__ = ["SessionStore"]
