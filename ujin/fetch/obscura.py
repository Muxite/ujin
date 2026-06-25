"""Obscura headless-browser wrapper — optional render fallback.

Vendored from jennie/services/scraper-v2/app/fetch/obscura.py.
Modifications:
- Removed jennie config dependency; reads OBSCURA_BIN / OBSCURA_URL from env.
- Added HTTP-mode (talks to ``obscura serve`` via aiohttp) alongside binary mode.
- Made binary/URL fully optional: callers check ``obscura_available()`` first.
- Logger changed to awork.scrape.obscura.

Usage in awork:
    Set OBSCURA_BIN=/path/to/obscura  (binary mode, default "obscura")
    OR
    Set OBSCURA_URL=http://localhost:9222  (HTTP service mode)

When neither is available, ObscuraError is raised so callers can degrade
gracefully to plain HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ObscuraError(RuntimeError):
    pass


class ObscuraTimeout(ObscuraError):
    pass


@dataclass
class ObscuraResult:
    url: str
    html: str
    elapsed_ms: int


def _bundled_binary() -> Optional[str]:
    """Path to the obscura binary built from the bundled ``ujin/obscura``
    submodule, if it exists. ``ujin obscura-build`` produces it."""
    candidate = (
        Path(__file__).resolve().parents[1] / "obscura" / "target" / "release" / "obscura"
    )
    return str(candidate) if candidate.is_file() else None


def _obscura_url() -> Optional[str]:
    return os.environ.get("OBSCURA_URL")


def _obscura_bin() -> str:
    """Resolve the binary path. Order: explicit OBSCURA_BIN env, then the
    bundled submodule build, then bare ``obscura`` (found on PATH)."""
    explicit = os.environ.get("OBSCURA_BIN")
    if explicit:
        return explicit
    bundled = _bundled_binary()
    if bundled:
        return bundled
    return "obscura"


def obscura_available() -> bool:
    """Synchronous check: is obscura reachable?

    True when an HTTP service URL is configured, or a binary resolves —
    explicit OBSCURA_BIN, the bundled submodule build, or ``obscura`` on PATH.
    """
    import shutil

    if _obscura_url():
        return True  # assume HTTP endpoint is up when URL is configured
    explicit = os.environ.get("OBSCURA_BIN")
    if explicit:
        return shutil.which(explicit) is not None or Path(explicit).is_file()
    if _bundled_binary():
        return True
    return shutil.which("obscura") is not None


class ObscuraFetcher:
    """Render a URL via obscura and return the resulting HTML.

    Prefers HTTP service mode (OBSCURA_URL) over binary mode (OBSCURA_BIN).
    Raises ObscuraError when neither is available.
    """

    def __init__(
        self,
        timeout_secs: int = 30,
    ):
        self._timeout = timeout_secs

    async def render_html(self, url: str) -> ObscuraResult:
        service_url = _obscura_url()
        if service_url:
            return await self._render_via_http(url, service_url)
        return await self._render_via_binary(url)

    async def _render_via_http(self, url: str, service_url: str) -> ObscuraResult:
        """POST to an obscura HTTP service (``obscura serve``)."""
        try:
            import aiohttp
        except ImportError as exc:
            raise ObscuraError("aiohttp required for obscura HTTP mode") from exc

        endpoint = service_url.rstrip("/") + "/render"
        loop = asyncio.get_running_loop()
        start = loop.time()
        timeout = aiohttp.ClientTimeout(total=self._timeout + 5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json={"url": url}) as resp:
                    if resp.status != 200:
                        raise ObscuraError(
                            f"obscura service returned HTTP {resp.status} for {url}"
                        )
                    data = await resp.json()
                    html = data.get("html", "")
        except aiohttp.ClientError as exc:
            raise ObscuraError(f"obscura HTTP service error for {url}: {exc}") from exc

        elapsed_ms = int((loop.time() - start) * 1000)
        return ObscuraResult(url=url, html=html, elapsed_ms=elapsed_ms)

    async def _render_via_binary(self, url: str) -> ObscuraResult:
        """Shell out to the obscura binary."""
        binary = _obscura_bin()
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "fetch",
                url,
                "--dump",
                "html",
                "--quiet",
                "--timeout",
                str(self._timeout),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise ObscuraError(
                f"obscura binary not found: {binary!r}. "
                "Set OBSCURA_BIN or OBSCURA_URL to enable headless rendering."
            ) from exc

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout + 5
            )
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                proc.kill()
            raise ObscuraTimeout(f"obscura timed out on {url}") from exc

        if proc.returncode != 0:
            raise ObscuraError(
                f"obscura exited {proc.returncode} fetching {url}"
            )

        elapsed_ms = int((loop.time() - start) * 1000)
        return ObscuraResult(
            url=url,
            html=stdout.decode(errors="replace"),
            elapsed_ms=elapsed_ms,
        )
