"""Cache layer: in-memory LRU+TTL, durable SQLite, per-host cooldown policy.

These are dependency-free (stdlib only).
"""
from __future__ import annotations

from .disk import DiskCache
from .hostpolicy import HostPolicy
from .store import CachedEntry, ScrapeCache

__all__ = ["ScrapeCache", "CachedEntry", "DiskCache", "HostPolicy"]
