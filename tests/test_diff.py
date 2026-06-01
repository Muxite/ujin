"""Site-change detection tests: region extraction, diffing, SitePollable."""
from __future__ import annotations

import pytest

from ujin.diff.detector import ChangeDetector
from ujin.diff.events import CallbackSink, ChangeEvent
from ujin.diff.region import extract_regions, region_fingerprints
from ujin.poll.base import PollResult

_HTML_A = """
<html><body>
  <main><h1>Top headline one</h1><h1>Second headline</h1></main>
  <footer>© 2026 random churn 12:00:01</footer>
</body></html>
"""

_HTML_B = """
<html><body>
  <main><h1>Top headline one</h1><h1>Second headline</h1></main>
  <footer>© 2026 random churn 13:45:22</footer>
</body></html>
"""

_HTML_C = """
<html><body>
  <main><h1>BREAKING new headline</h1><h1>Second headline</h1></main>
  <footer>© 2026 random churn 14:00:00</footer>
</body></html>
"""


def test_extract_regions_normalizes():
    regions = extract_regions(_HTML_A, ["main"])
    assert "Top headline one" in regions["main"]
    assert "Second headline" in regions["main"]


def test_region_fingerprint_ignores_unwatched_churn():
    """Footer timestamp churns but we only watch <main> → same fingerprint."""
    fp_a = region_fingerprints(_HTML_A, ["main"])
    fp_b = region_fingerprints(_HTML_B, ["main"])
    assert fp_a == fp_b  # main unchanged
    diff = ChangeDetector().diff(fp_a, fp_b)
    assert not diff.any


def test_region_fingerprint_detects_watched_change():
    fp_a = region_fingerprints(_HTML_A, ["main"])
    fp_c = region_fingerprints(_HTML_C, ["main"])
    assert fp_a != fp_c
    diff = ChangeDetector().diff(fp_a, fp_c)
    assert diff.changed == ["main"]
    assert diff.any


def test_detector_added_removed():
    diff = ChangeDetector().diff({"a": "1"}, {"a": "1", "b": "2"})
    assert diff.added == ["b"] and not diff.changed
    diff2 = ChangeDetector().diff({"a": "1", "b": "2"}, {"a": "1"})
    assert diff2.removed == ["b"]


async def test_sitepollable_drives_change_signal():
    """SitePollable fingerprints region map; engine-style decide_changed works."""
    from ujin.poll.site import SitePollable

    class FakeFetcher:
        def __init__(self, bodies):
            self._bodies = iter(bodies)

        async def get(self, url, *, etag=None, last_modified=None, extra_headers=None):
            from ujin.fetch.http import HttpResponse

            return HttpResponse(url=url, status=200, body=next(self._bodies),
                                final_url=url)

    fetcher = FakeFetcher([_HTML_A, _HTML_B, _HTML_C])
    site = SitePollable("https://x.test/", ["main"], fetcher=fetcher)

    r1 = await site.poll(None)
    assert r1.changed is True  # first poll counts as changed
    r2 = await site.poll(r1)
    assert r2.changed is False  # only footer churned; main stable
    assert r2.fingerprint == r1.fingerprint
    r3 = await site.poll(r2)
    assert r3.changed is True  # main headline changed
    assert r3.payload["region_diff"].changed == ["main"]


async def test_callback_sink_receives_event():
    seen = []
    sink = CallbackSink(lambda ev: seen.append(ev))
    from ujin.diff.detector import RegionDiff

    result = PollResult(
        ok=True, changed=True, fingerprint="abc",
        payload={"region_diff": RegionDiff(changed=["main"])},
    )
    await sink("https://x.test/", result)
    assert len(seen) == 1
    assert isinstance(seen[0], ChangeEvent)
    assert seen[0].regions["changed"] == ["main"]
