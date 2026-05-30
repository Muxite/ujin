"""RssPollable — poll an RSS/Atom feed for new or changed entries.

Fingerprints the set of entry URLs so adding/removing items flips ``changed``;
the payload includes which entry URLs are new since the previous poll, so a
consumer can act on just the new ones.
"""
from __future__ import annotations

import time

from eujin.poll.base import PollResult, decide_changed, fingerprint


class RssPollable:
    def __init__(self, url: str, *, key: str | None = None, timeout: float = 20.0) -> None:
        self.url = url
        self.key = key or url
        self.timeout = timeout

    async def poll(self, prev: PollResult | None) -> PollResult:
        try:
            from eujin.sources.rss import parse_feed
        except ImportError:
            return PollResult.failure("feedparser required: pip install 'eujin[web]'")

        start = time.monotonic()
        try:
            items = await parse_feed(self.url, timeout_secs=int(self.timeout))
        except Exception as exc:  # noqa: BLE001
            return PollResult.failure(f"{type(exc).__name__}: {exc}")

        urls = [it.url for it in items]
        fp = fingerprint(urls)
        prev_urls = set()
        if prev and isinstance(prev.payload, dict):
            prev_urls = set(prev.payload.get("urls", []))
        new_urls = [u for u in urls if u not in prev_urls]
        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload={"urls": urls, "new_urls": new_urls,
                     "items": [it.__dict__ for it in items]},
            latency_ms=int((time.monotonic() - start) * 1000),
        )
