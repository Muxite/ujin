"""ScrapePollable — drive the rich :class:`ScrapeService` as a poll source.

This is what lets a *job* use the full fetch→obscura→sitemap→RSS fallback chain
(and extraction: links/article/structured) as a change-detected source, rather
than the bare HTTP fetch of :class:`ujin.poll.http.HttpPollable`.

``ScrapeResult`` already carries a content ``fingerprint``, so change detection
is identical to every other role: compare to the previous poll's fingerprint.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ujin.poll.base import PollResult, decide_changed

if TYPE_CHECKING:
    from ujin.scrape.service import ScrapeService


class ScrapePollable:
    """Poll a URL through a shared :class:`ScrapeService`."""

    def __init__(
        self,
        service: "ScrapeService",
        url: str,
        *,
        mode: str = "links",
        force_refresh: bool = False,
        key: str | None = None,
    ):
        self._svc = service
        self.url = url
        self.mode = mode
        self.force_refresh = force_refresh
        self.key = key or f"scrape:{mode}:{url}"

    async def poll(self, prev: PollResult | None) -> PollResult:
        started = time.monotonic()
        try:
            r = await self._svc.scrape(
                self.url, mode=self.mode, force_refresh=self.force_refresh
            )
        except Exception as exc:  # noqa: BLE001
            return PollResult.failure(f"{type(exc).__name__}: {exc}")
        latency_ms = int((time.monotonic() - started) * 1000)
        return PollResult(
            ok=True,
            changed=decide_changed(r.fingerprint, prev),
            fingerprint=r.fingerprint,
            payload=r,
            latency_ms=latency_ms,
            status=200,
            retry_after=r.next_poll_hint_secs,
        )
