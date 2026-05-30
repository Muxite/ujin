"""ApiPollable — poll a JSON endpoint and fingerprint a selected slice.

Fetch JSON (GET/POST), optionally narrow to a dotted ``json_path`` (e.g.
``data.items``), and fingerprint that slice so unrelated fields (timestamps,
request ids) don't trigger false "changed". Honors ``Retry-After`` on 429.
"""
from __future__ import annotations

import time
from typing import Any

from eujin.poll.base import PollResult, decide_changed, fingerprint


def _dig(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


class ApiPollable:
    def __init__(
        self,
        url: str,
        *,
        key: str | None = None,
        method: str = "GET",
        json_path: str | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout: float = 20.0,
    ) -> None:
        self.url = url
        self.key = key or f"{method}:{url}:{json_path or ''}"
        self.method = method.upper()
        self.json_path = json_path
        self.headers = headers or {}
        self.json_body = json_body
        self.timeout = timeout

    async def poll(self, prev: PollResult | None) -> PollResult:
        try:
            import aiohttp
        except ImportError:
            return PollResult.failure("aiohttp required: pip install 'eujin[web]'")

        start = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    self.method, self.url, headers=self.headers, json=self.json_body
                ) as resp:
                    latency = int((time.monotonic() - start) * 1000)
                    if resp.status == 429 or resp.status >= 500:
                        ra = resp.headers.get("Retry-After")
                        return PollResult(
                            ok=False, status=resp.status, error=f"http {resp.status}",
                            retry_after=float(ra) if ra and ra.isdigit() else None,
                            latency_ms=latency,
                        )
                    data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            return PollResult.failure(f"{type(exc).__name__}: {exc}")

        slice_ = _dig(data, self.json_path)
        fp = fingerprint(slice_)
        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload=slice_,
            status=200,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
