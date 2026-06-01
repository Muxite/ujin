"""Site-change detection: selector/region-scoped diffing over HTML snapshots.

Layered on top of the engine's existing fingerprint/decide_changed machinery
rather than replacing it. ``region_fingerprints`` hashes only the parts of a
page you care about (a headline list, a price, a status banner) so cosmetic
churn elsewhere doesn't read as a change. :class:`ChangeDetector` reports
*which* regions moved, and the sinks in :mod:`ujin.diff.events` deliver those
change events to a callback or a webhook.

Pulls the ``diff`` extra (selectolax for region extraction, aiohttp for the
webhook sink) — imported lazily.
"""
from __future__ import annotations

from .detector import ChangeDetector, RegionDiff
from .events import CallbackSink, ChangeEvent, WebhookSink
from .region import extract_regions, region_fingerprints

__all__ = [
    "extract_regions",
    "region_fingerprints",
    "ChangeDetector",
    "RegionDiff",
    "ChangeEvent",
    "CallbackSink",
    "WebhookSink",
]
