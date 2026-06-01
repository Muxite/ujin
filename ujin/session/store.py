"""Persistent cookie store backed by aiohttp's CookieJar.

The jar is shared with the :class:`~ujin.fetch.http.HttpFetcher` session so
cookies set by a response are sent on subsequent requests. ``save``/``load``
persist the jar to disk (aiohttp pickles it) so sessions survive restarts.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("ujin.session")


class SessionStore:
    def __init__(self, path: Optional[str | Path] = None, *, unsafe: bool = False):
        """``path`` enables on-disk persistence (optional). ``unsafe=True``
        keeps cookies for IP-address hosts (aiohttp default drops them)."""
        import aiohttp

        self._path = Path(path) if path else None
        self._jar = aiohttp.CookieJar(unsafe=unsafe)
        if self._path is not None and self._path.exists():
            try:
                self._jar.load(self._path)
                log.info("loaded cookie jar from %s", self._path)
            except Exception as exc:  # noqa: BLE001
                log.warning("cookie jar load failed (%s); starting empty", exc)

    @property
    def jar(self) -> Any:
        """The aiohttp CookieJar to hand to a ClientSession."""
        return self._jar

    def save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._jar.save(self._path)
        except Exception as exc:  # noqa: BLE001
            log.warning("cookie jar save to %s failed: %s", self._path, exc)

    def clear(self) -> None:
        self._jar.clear()
