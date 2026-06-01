"""HttpPollable — poll a web page for change.

Uses :class:`ujin.fetch.http.HttpFetcher` (per-host concurrency + conditional
GET). On each poll it sends the previous ETag/Last-Modified; an HTTP 304 is a
cheap "unchanged". Otherwise the body is fingerprinted and compared. Optional
obscura render for JS/anti-bot pages.
"""
from __future__ import annotations

import time

from ujin.poll.base import PollResult, decide_changed, fingerprint


class HttpPollable:
    def __init__(
        self,
        url: str,
        *,
        key: str | None = None,
        fetcher=None,
        render: bool = False,
    ) -> None:
        self.url = url
        self.key = key or url
        self._fetcher = fetcher  # shared HttpFetcher; created lazily if None
        self._owns_fetcher = fetcher is None
        self.render = render

    async def _get_fetcher(self):
        if self._fetcher is None:
            from ujin.fetch.http import HttpFetcher

            self._fetcher = HttpFetcher()
            await self._fetcher.start()
        return self._fetcher

    async def poll(self, prev: PollResult | None) -> PollResult:
        start = time.monotonic()
        extra = (prev.payload or {}) if prev else {}
        etag = extra.get("etag") if isinstance(extra, dict) else None
        last_mod = extra.get("last_modified") if isinstance(extra, dict) else None

        try:
            if self.render:
                from ujin.fetch.obscura import ObscuraFetcher

                html = await ObscuraFetcher().render_html(self.url)
                body, status, et, lm = html, 200, None, None
                not_modified = False
            else:
                fetcher = await self._get_fetcher()
                resp = await fetcher.get(self.url, etag=etag, last_modified=last_mod)
                body, status = resp.body, resp.status
                et, lm, not_modified = resp.etag, resp.last_modified, resp.not_modified
        except Exception as exc:  # noqa: BLE001
            return PollResult.failure(f"{type(exc).__name__}: {exc}")

        latency = int((time.monotonic() - start) * 1000)
        if status == 429 or status >= 500:
            return PollResult(ok=False, status=status, error=f"http {status}",
                              latency_ms=latency)

        if not_modified:  # 304: cheap unchanged, keep prior fingerprint
            fp = prev.fingerprint if prev else None
            return PollResult(ok=True, changed=False, fingerprint=fp,
                              payload={"etag": etag, "last_modified": last_mod,
                                       "not_modified": True},
                              status=304, latency_ms=latency)

        fp = fingerprint(body)
        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload={"body": body, "etag": et, "last_modified": lm},
            status=status,
            latency_ms=latency,
        )
