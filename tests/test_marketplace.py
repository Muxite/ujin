"""Site-profile marketplace source — registration + profile-driven sampling (no network)."""
from __future__ import annotations

from ujin.poll.marketplace import SITE_PROFILES, MarketplaceSearchPollable
from ujin.registry import register


def test_registered_builtin():
    assert register.has("source", "marketplace_search")


def test_newegg_profile_has_component_keyterms():
    kt = SITE_PROFILES["newegg"]["keyterms"]
    assert set(kt) == {"RAM", "SSD", "HDD"}
    assert SITE_PROFILES["newegg"]["selectors"]["card"] == ".item-cell"


def test_sample_draws_from_profile_keyterms_with_category():
    src = MarketplaceSearchPollable(profile="newegg", terms_per_poll=3, seed=1)
    pairs = src._sample()
    assert len(pairs) == 3
    valid = {(t, cat) for cat, terms in SITE_PROFILES["newegg"]["keyterms"].items() for t in terms}
    assert all(p in valid for p in pairs)


def test_child_pollable_uses_profile_url_and_source():
    src = MarketplaceSearchPollable(profile="newegg", seed=1)
    child = src._child("ddr5 ram", "RAM")
    assert child.source == "newegg"
    assert "newegg.com" in child.search_url
    assert child.category == "RAM"


def test_unknown_profile_falls_back_to_amazon():
    src = MarketplaceSearchPollable(profile="nope")
    assert src.profile_name == "amazon"
