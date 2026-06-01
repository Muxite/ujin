"""Async HTTP fetcher with shared session, per-host concurrency, and
conditional GET (ETag / If-Modified-Since) support.

Vendored from jennie/services/scraper-v2/app/fetch/http.py.
Modifications: removed dependency on jennie config/settings; uses local
defaults instead. Logger name changed to awork.scrape.http.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

# aiohttp is an optional dependency (scrape extra)
try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

_USER_AGENT = "Mozilla/5.0 (compatible; awork/1.0; +https://github.com/awork)"
_PER_HOST_CONCURRENCY = 2
_HTTP_TIMEOUT_SECS = 20


@dataclass
class HttpResponse:
    url: str
    status: int
    body: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False
    elapsed_ms: int = 0
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class HttpFetcher:
    """Shared aiohttp.ClientSession with a per-host semaphore.

    Why: aiohttp's TCPConnector pools sockets, so reusing one session
    across requests cuts TLS handshake latency by ~80% on warm sites.
    The per-host semaphore prevents stampedes against a single origin.
    """

    def __init__(
        self,
        per_host_concurrency: int = _PER_HOST_CONCURRENCY,
        timeout_secs: int = _HTTP_TIMEOUT_SECS,
        user_agent: str = _USER_AGENT,
    ):
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for HTTP fetching: "
                "pip install 'awork[scrape]'"
            )
        self._timeout = aiohttp.ClientTimeout(total=timeout_secs)
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        self._session: Optional[aiohttp.ClientSession] = None
        self._per_host_limit = per_host_concurrency
        self._host_locks: dict[str, asyncio.Semaphore] = {}
        self._lock_creation = asyncio.Lock()

    async def start(self, *, session: Optional["object"] = None) -> None:
        """Open the shared session.

        ``session`` may be a :class:`ujin.session.store.SessionStore` (or any
        object exposing a ``.jar`` aiohttp CookieJar) to persist cookies across
        requests/runs. Stored on ``self`` so callers can ``.save()`` it later.
        """
        self._session_store = session
        if self._session is None:
            connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=300)
            cookie_jar = getattr(session, "jar", None)
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=self._timeout, headers=self._headers,
                cookie_jar=cookie_jar,
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _host_sem(self, host: str) -> asyncio.Semaphore:
        sem = self._host_locks.get(host)
        if sem is None:
            async with self._lock_creation:
                sem = self._host_locks.get(host)
                if sem is None:
                    sem = asyncio.Semaphore(self._per_host_limit)
                    self._host_locks[host] = sem
        return sem

    async def get(
        self,
        url: str,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        extra_headers: Optional[dict[str, str]] = None,
        proxy: Optional[str] = None,
    ) -> HttpResponse:
        """Issue GET with optional conditional headers.

        ``proxy`` (e.g. from :class:`ujin.proxy.pool.ProxyPool`) routes this
        request through an upstream proxy. Returns body='' and
        not_modified=True on HTTP 304.
        """
        if self._session is None:
            await self.start()
        assert self._session is not None

        host = urlsplit(url).netloc
        sem = await self._host_sem(host)

        cond_headers: dict[str, str] = {}
        if etag:
            cond_headers["If-None-Match"] = etag
        if last_modified:
            cond_headers["If-Modified-Since"] = last_modified
        if extra_headers:
            cond_headers.update(extra_headers)

        loop = asyncio.get_running_loop()
        start = loop.time()
        async with sem:
            async with self._session.get(
                url, headers=cond_headers or None, allow_redirects=True,
                proxy=proxy,
            ) as resp:
                elapsed_ms = int((loop.time() - start) * 1000)
                if resp.status == 304:
                    return HttpResponse(
                        url=url,
                        status=304,
                        body="",
                        not_modified=True,
                        elapsed_ms=elapsed_ms,
                        final_url=str(resp.url),
                        headers=dict(resp.headers),
                    )
                body = await resp.text(errors="replace")
                return HttpResponse(
                    url=url,
                    status=resp.status,
                    body=body,
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                    elapsed_ms=elapsed_ms,
                    final_url=str(resp.url),
                    headers=dict(resp.headers),
                )

    async def __aenter__(self) -> "HttpFetcher":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
