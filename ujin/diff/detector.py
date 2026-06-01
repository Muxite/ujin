"""Structured diff between two region-fingerprint snapshots."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RegionDiff:
    """Which selectors changed between two snapshots.

    * ``changed``  — selector present in both, fingerprint differs.
    * ``added``    — selector now produces content it didn't before
                     (newly matched, or went from empty to non-empty).
    * ``removed``  — selector no longer present in the new snapshot.
    """

    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.changed or self.added or self.removed)

    def as_dict(self) -> dict[str, list[str]]:
        return {"changed": self.changed, "added": self.added, "removed": self.removed}


class ChangeDetector:
    """Compare region-fingerprint maps to produce a :class:`RegionDiff`."""

    def diff(
        self, prev_fps: dict[str, str] | None, new_fps: dict[str, str]
    ) -> RegionDiff:
        prev_fps = prev_fps or {}
        diff = RegionDiff()
        for sel, fp in new_fps.items():
            if sel not in prev_fps:
                diff.added.append(sel)
            elif prev_fps[sel] != fp:
                diff.changed.append(sel)
        for sel in prev_fps:
            if sel not in new_fps:
                diff.removed.append(sel)
        return diff
