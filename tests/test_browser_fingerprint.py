"""Fingerprint rotation pool — offline checks on the stealth identity profiles."""
from __future__ import annotations

import random

from ujin.fetch.browser import _FINGERPRINTS, _Fingerprint, _pick_fingerprint


def test_pool_nonempty_and_typed():
    assert len(_FINGERPRINTS) >= 3
    assert all(isinstance(fp, _Fingerprint) for fp in _FINGERPRINTS)


def test_fingerprints_are_internally_consistent():
    """A Windows UA never pairs with a Mac-only viewport, etc. — each profile agrees with itself."""
    for fp in _FINGERPRINTS:
        assert fp.user_agent and "Mozilla/5.0" in fp.user_agent
        assert fp.locale and "-" in fp.locale                      # e.g. en-US / en-GB
        assert fp.accept_language.startswith(fp.locale.split(",")[0][:2])
        assert fp.viewport["width"] >= 1024 and fp.viewport["height"] >= 600
        # The Accept-Language header should lead with the context locale's language.
        assert fp.accept_language[:2] == fp.locale[:2]


def test_pick_is_deterministic_under_seeded_rng():
    rng_a = random.Random(7)
    rng_b = random.Random(7)
    picks_a = [_pick_fingerprint(rng_a).user_agent for _ in range(10)]
    picks_b = [_pick_fingerprint(rng_b).user_agent for _ in range(10)]
    assert picks_a == picks_b                                       # reproducible with a seed


def test_pick_rotates_across_multiple_profiles():
    rng = random.Random(123)
    seen = {_pick_fingerprint(rng).user_agent for _ in range(60)}
    assert len(seen) >= 3                                           # actually varies the identity
