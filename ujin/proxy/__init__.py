"""Proxy rotation for the fetch layer.

A :class:`ProxyPool` hands out proxy URLs round-robin, skips ones that have
recently failed, and recovers them after a cooldown. Dependency-free.
"""
from __future__ import annotations

from .pool import ProxyPool

__all__ = ["ProxyPool"]
