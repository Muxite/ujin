"""Compatibility shim — the API-key middleware now guards all three services
and lives at :mod:`ujin.auth`."""
from __future__ import annotations

from ujin.auth import ApiKeyMiddleware, _present_key  # noqa: F401

__all__ = ["ApiKeyMiddleware"]
