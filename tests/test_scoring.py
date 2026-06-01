"""Trends scoring tests: corroboration, tier/breaking_score, BreakingScorer.

These lock the news-trading semantics so the later jennie migration is a
behavior-preserving swap. All pure-python — no network.
"""
from __future__ import annotations

from ujin.extract.links import NormalizedLink
from ujin.scrape.host_overrides import HostOverrideRegistry
from ujin.trends import (
    BreakingScorer,
    CorroborationStore,
    Weights,
    breaking_score,
    tier_of,
)
from ujin.trends.corroboration import jaccard, shingleset


def test_shingle_jaccard_basics():
    a = shingleset("Senate passes spending bill in late-night vote")
    b = shingleset("Senate approves spending bill after late vote")
    assert a and b
    assert 0.0 < jaccard(a, b) <= 1.0
    assert jaccard(a, frozenset()) == 0.0


def test_corroboration_three_hosts_scores():
    store = CorroborationStore(min_hosts_for_corroboration=3, max_hosts_for_full_score=5)
    headline = "Senate passes major spending bill in late-night vote"
    store.add(headline, "apnews.com")
    store.add("Senate approves spending bill after late-night vote", "reuters.com")
    store.add("Senate passes spending bill in a late vote", "bbc.com")
    score, n = store.lookup_score(headline, "cnn.com")
    assert n >= 3
    assert score >= 0.5
    clusters = store.clusters()
    assert any(len(c.hosts) >= 3 for c in clusters)


def test_tier_of_defaults():
    assert tier_of("apnews.com", None) == "wire"
    assert tier_of("example.com", None) == "mainstream"
    assert tier_of("example.com", "specialty") == "specialty"


def test_breaking_score_wire_with_lede():
    score, components = breaking_score(
        url="https://apnews.com/article/x",
        title="BREAKING: Senate passes spending bill",
        tier_label="wire",
        weights=Weights(),
    )
    assert 0.0 < score <= 1.0
    assert components["source_rank"] > 0
    assert components["lede_marker"] > 0
    # No corroboration store -> zero contribution.
    assert components["corroboration"] == 0.0


def test_breaking_scorer_annotates_links_and_hints():
    overrides = HostOverrideRegistry.from_dict(
        {"hosts": {"apnews.com": {"tier": "wire"}}}
    )
    scorer = BreakingScorer(overrides=overrides, breaking_threshold=0.40)
    links = [
        NormalizedLink(url="https://apnews.com/article/abc", text="A serious headline here"),
    ]
    scorer.score_links(links, base_url="https://apnews.com/")
    assert getattr(links[0], "_tier") == "wire"
    assert isinstance(getattr(links[0], "_breaking_score"), float)
    assert isinstance(getattr(links[0], "_score_components"), dict)

    # Wire tier -> base hint 30; unchanged doubles to 60.
    assert scorer.poll_hint([], base_url="https://apnews.com/", prior_unchanged=False) == 30.0
    assert scorer.poll_hint([], base_url="https://apnews.com/", prior_unchanged=True) == 60.0


def test_breaking_scorer_breaking_link_lowers_hint():
    overrides = HostOverrideRegistry()  # no wire tier
    scorer = BreakingScorer(overrides=overrides, breaking_threshold=0.40)
    link = NormalizedLink(url="https://x.test/a", text="headline")
    object.__setattr__(link, "_breaking_score", 0.9)
    hint = scorer.poll_hint([link], base_url="https://x.test/", prior_unchanged=False)
    assert hint == 20.0
