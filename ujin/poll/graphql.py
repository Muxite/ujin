"""GraphQLPollable — POST a GraphQL query and fingerprint a selected slice.

Issues a POST with ``{"query": ..., "variables": ...}`` to *url*, narrows the
response JSON to a dotted ``data_path`` (e.g. ``data.users``) for fingerprinting
so unrelated fields (request ids, timestamps) don't trigger false "changed".
A GraphQL ``errors`` array in the response is surfaced as a poll failure without
crashing the poll loop; non-200 status codes and network exceptions are handled
the same way.
"""
from __future__ import annotations

import time
from typing import Any

from ujin.poll.api import _dig
from ujin.poll.base import PollResult, decide_changed, fingerprint


class GraphQLPollable:
    def __init__(
        self,
        url: str,
        *,
        query: str,
        variables: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data_path: str | None = None,
        key: str | None = None,
        timeout: float = 20.0,
        _fetcher=None,
    ) -> None:
        self.url = url
        self.query = query
        self.variables = variables or {}
        self.headers = headers or {}
        self.data_path = data_path
        self.key = key or f"graphql:{url}:{data_path or ''}"
        self.timeout = timeout
        self._fetcher = _fetcher

    async def poll(self, prev: PollResult | None) -> PollResult:
        payload: dict[str, Any] = {"query": self.query}
        if self.variables:
            payload["variables"] = self.variables

        if self._fetcher is not None:
            fetch = self._fetcher
        else:
            try:
                import aiohttp
            except ImportError:
                return PollResult.failure("aiohttp required: pip install 'ujin[web]'")

            async def fetch(url: str, body: Any, hdrs: dict, tout: float):  # type: ignore[misc]
                t = aiohttp.ClientTimeout(total=tout)
                async with aiohttp.ClientSession(timeout=t) as session:
                    async with session.post(url, json=body, headers=hdrs) as resp:
                        return resp.status, await resp.json(content_type=None)

        start = time.monotonic()
        try:
            status, data = await fetch(self.url, payload, self.headers, self.timeout)
        except Exception as exc:  # noqa: BLE001
            return PollResult.failure(f"{type(exc).__name__}: {exc}")

        latency = int((time.monotonic() - start) * 1000)

        if status == 429 or status >= 500:
            return PollResult(
                ok=False, status=status, error=f"http {status}", latency_ms=latency,
            )

        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            msgs = "; ".join(
                e.get("message", "?") if isinstance(e, dict) else str(e)
                for e in errors
            )
            return PollResult.failure(f"GraphQL errors: {msgs}", status=status)

        slice_ = _dig(data, self.data_path)
        fp = fingerprint(slice_)
        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload=slice_,
            status=status,
            latency_ms=latency,
        )
