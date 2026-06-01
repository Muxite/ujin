"""SitePollable — watch specific regions of a web page for change.

Like :class:`ujin.poll.http.HttpPollable`, but instead of fingerprinting the
whole body it fingerprints only the regions matched by a list of CSS selectors,
so cosmetic churn elsewhere on the page doesn't trip the watcher. The top-level
``PollResult.fingerprint`` is a hash of the region map, so the engine's existing
``decide_changed`` + ``AdaptiveInterval`` adapt cadence exactly as for any other
pollable. A :class:`ujin.diff.detector.RegionDiff` is attached to the payload so
``on_change`` sinks can report *which* region moved.

With no selectors it degrades to whole-page fingerprinting (same as
HttpPollable), so it is a strict superset.
"""
from __future__ import annotations

import time

from ujin.poll.base import PollResult, decide_changed, fingerprint


class SitePollable:
    def __init__(
        self,
        url: str,
        selectors: list[str] | None = None,
        *,
        key: str | None = None,
        fetcher=None,
        render: bool = False,
    ) -> None:
        self.url = url
        self.selectors = list(selectors or [])
        self.key = key or url
        self._fetcher = fetcher
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
        extra = extra if isinstance(extra, dict) else {}
        etag = extra.get("etag")
        last_mod = extra.get("last_modified")

        try:
            if self.render:
                from ujin.fetch.obscura import ObscuraFetcher

                rendered = await ObscuraFetcher().render_html(self.url)
                body, status = rendered.html, 200
                et, lm, not_modified = None, None, False
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

        if not_modified:  # 304: cheap unchanged, keep prior region map
            return PollResult(
                ok=True, changed=False,
                fingerprint=prev.fingerprint if prev else None,
                payload={"regions": extra.get("regions", {}),
                         "etag": etag, "last_modified": last_mod,
                         "not_modified": True},
                status=304, latency_ms=latency,
            )

        from ujin.diff.detector import ChangeDetector
        from ujin.diff.region import region_fingerprints

        if self.selectors:
            new_regions = region_fingerprints(body, self.selectors)
            fp = fingerprint(new_regions)
        else:
            # No selectors → whole-page fingerprint (HttpPollable parity).
            new_regions = {}
            fp = fingerprint(body)

        prev_regions = extra.get("regions") if isinstance(extra, dict) else None
        region_diff = ChangeDetector().diff(prev_regions, new_regions)

        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload={"regions": new_regions, "region_diff": region_diff,
                     "etag": et, "last_modified": lm},
            status=status,
            latency_ms=latency,
        )
