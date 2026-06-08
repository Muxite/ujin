"""Tests for the randomized category source's term sampling (no network)."""
from __future__ import annotations

from ujin.poll.amazon import _KEYTERM_BANK, AmazonCategoryPollable
from ujin.registry import register


def test_registered_builtin():
    assert register.has("source", "amazon_category")


def test_sample_respects_category_and_count():
    src = AmazonCategoryPollable(categories=["Kitchen"], terms_per_poll=3,
                                 mutate_prob=0.0, seed=1)
    pairs = src._sample_terms()
    assert len(pairs) == 3
    assert all(cat == "Kitchen" for _, cat in pairs)
    # No mutation -> every term is a bank term verbatim.
    assert all(term in _KEYTERM_BANK["Kitchen"] for term, _ in pairs)


def test_sampling_is_random_across_polls():
    src = AmazonCategoryPollable(terms_per_poll=4, mutate_prob=0.0, seed=7)
    first = src._sample_terms()
    second = src._sample_terms()
    # Distinct draws over the full bank are very unlikely to be identical.
    assert first != second


def test_mutation_can_alter_terms():
    src = AmazonCategoryPollable(categories=["Tools"], terms_per_poll=5,
                                 mutate_prob=1.0, seed=3)
    pairs = src._sample_terms()
    # With mutate_prob=1.0 at least one term should differ from the bare bank term.
    assert any(term not in _KEYTERM_BANK["Tools"] for term, _ in pairs)


def test_unknown_categories_fall_back_to_all():
    src = AmazonCategoryPollable(categories=["NoSuchCategory"], seed=1)
    assert set(src.categories) == set(_KEYTERM_BANK)
