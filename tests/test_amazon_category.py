"""Tests for the randomized category source's term sampling (no network)."""
from __future__ import annotations

from ujin.poll.amazon import _HarvestStore, _KEYTERM_BANK, AmazonCategoryPollable
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


# ── Harvesting ──────────────────────────────────────────────────────────────

def test_harvest_extracts_uncommon_words_and_skips_stopwords(tmp_path):
    store = _HarvestStore(str(tmp_path / "h.json"))
    store.add_from_titles(
        [{"title": "Stainless Steel Safety Razor with Blades", "category": "Beauty"}],
        min_len=4,
    )
    # Long content words are learned; short words and stopwords ("with") are not.
    assert "stainless" in store.pool and store.pool["stainless"] == "Beauty"
    assert "razor" in store.pool and "blades" in store.pool
    assert "with" not in store.pool  # stopword
    assert "steel" in store.pool


def test_harvest_never_repeats_a_used_term(tmp_path):
    store = _HarvestStore(str(tmp_path / "h.json"), rng=__import__("random").Random(0))
    store.add_from_titles([{"title": "cordless impact driver", "category": "Tools"}], min_len=4)
    drawn = store.draw(3)
    drawn_terms = {t for t, _ in drawn}
    assert drawn_terms and drawn_terms <= {"cordless", "impact", "driver"}
    # Drawn terms move to `used` and never re-enter the pool, even if re-harvested.
    assert drawn_terms <= store.used
    assert not (drawn_terms & set(store.pool))
    store.add_from_titles([{"title": "cordless impact driver", "category": "Tools"}], min_len=4)
    assert not (drawn_terms & set(store.pool))


def test_harvest_store_persists_across_instances(tmp_path):
    path = str(tmp_path / "h.json")
    s1 = _HarvestStore(path)
    s1.add_from_titles([{"title": "bamboo cutting board", "category": "Kitchen"}], min_len=4)
    s1.save()
    s2 = _HarvestStore(path)
    assert "bamboo" in s2.pool and s2.pool["bamboo"] == "Kitchen"


def test_poll_draws_from_harvest_pool(tmp_path):
    path = str(tmp_path / "h.json")
    # Seed a pool the sampler can draw from.
    seed_store = _HarvestStore(path)
    seed_store.add_from_titles([{"title": "merino wool hiking socks", "category": "Apparel"}], min_len=4)
    seed_store.save()

    src = AmazonCategoryPollable(terms_per_poll=2, mutate_prob=0.0, seed=2,
                                 harvest=True, harvest_path=path, harvest_ratio=1.0)
    pairs = src._sample_terms()
    assert len(pairs) == 2
    # With ratio 1.0, at least one term comes from the harvested pool.
    assert any(term in {"merino", "wool", "hiking", "socks"} for term, _ in pairs)
