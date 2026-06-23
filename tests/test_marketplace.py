"""Generic marketplace engine + externally-supplied site profiles (no network).

ujin ships no built-in profiles; these tests supply them inline or load the shipped
reference file (which doubles as validation of examples/marketplace_profiles.yaml).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ujin.poll.marketplace import (
    PROFILES_ENV,
    MarketplaceSearchPollable,
    load_profiles,
)
from ujin.registry import register

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "marketplace_profiles.yaml"

INLINE = {
    "shop": {
        "domain": "shop.example",
        "search_url": "https://www.{domain}/find?q={query}",
        "engine": "http",
        "keyterms": {"Cat": ["alpha", "beta", "gamma"]},
    }
}


def test_registered_builtin():
    assert register.has("source", "marketplace_search")


# ── profile loading (file / inline / env / precedence) ─────────────────────────
def test_load_profiles_from_file():
    profs = load_profiles(path=EXAMPLE)
    assert {"amazon", "newegg", "ebay", "walmart"} <= set(profs)
    assert profs["newegg"]["selectors"]["card"] == ".item-cell"
    assert "{query}" in profs["ebay"]["search_url"]


def test_load_profiles_inline_overrides_file():
    merged = load_profiles(inline={"newegg": {"domain": "x", "search_url": "y"}}, path=EXAMPLE)
    assert merged["newegg"]["domain"] == "x"      # inline wins
    assert "ebay" in merged                          # file entries still present


def test_load_profiles_from_env(monkeypatch):
    monkeypatch.setenv(PROFILES_ENV, str(EXAMPLE))
    assert "walmart" in load_profiles()


def test_no_source_yields_no_profiles():
    assert load_profiles() == {}


# ── unknown profile is a hard error (no built-in fallback) ─────────────────────
def test_unknown_profile_raises_with_profiles():
    with pytest.raises(ValueError, match="unknown marketplace profile"):
        MarketplaceSearchPollable(profile="nope", profiles=INLINE)


def test_missing_profiles_raises():
    with pytest.raises(ValueError, match="unknown marketplace profile"):
        MarketplaceSearchPollable(profile="amazon")  # nothing supplied


# ── engine behaviour, driven by a supplied profile ────────────────────────────
def test_sample_draws_from_profile_keyterms():
    src = MarketplaceSearchPollable(profile="newegg", profiles_path=str(EXAMPLE),
                                    terms_per_poll=3, seed=1)
    pairs = src._sample()
    valid = {(t, cat) for cat, terms in src.profile["keyterms"].items() for t in terms}
    assert len(pairs) == 3 and all(p in valid for p in pairs)


def test_child_uses_profile_url_and_source_inline():
    src = MarketplaceSearchPollable(profile="shop", profiles=INLINE, seed=1)
    child = src._child("alpha", "Cat")
    assert child.source == "shop"
    assert "shop.example/find?q=" in child.search_url


def test_ebay_child_uses_profile_url(monkeypatch):
    src = MarketplaceSearchPollable(profile="ebay", profiles_path=str(EXAMPLE), seed=1)
    child = src._child("wireless earbuds", "Electronics")
    assert child.source == "ebay"
    assert "ebay.com/sch/?_nkw=" in child.search_url
    assert "wireless+earbuds" in child.search_url


async def test_poll_combines_and_dedupes(monkeypatch):
    from ujin.poll import amazon as amz
    from ujin.poll.base import PollResult

    calls: list[str] = []

    async def fake_child_poll(self, prev):
        calls.append(self.term)
        return PollResult(ok=True, changed=True, payload=[
            {"source": self.source, "source_id": f"u-{self.term[:4]}", "title": self.term, "price_cents": 100},
            {"source": self.source, "source_id": "shared", "title": "dupe", "price_cents": 200},
        ])

    monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_child_poll)
    src = MarketplaceSearchPollable(profile="newegg", profiles_path=str(EXAMPLE),
                                    terms_per_poll=3, seed=1)
    res = await src.poll(None)
    assert res.ok and res.payload
    assert len(calls) == 3
    ids = [it["source_id"] for it in res.payload]
    assert ids.count("shared") == 1                 # deduped across children


async def test_amazon_search_poll_stamps_category_without_network(monkeypatch):
    from ujin.poll.amazon import AmazonSearchPollable
    from ujin.extract import product as prod

    async def fake_render(self, url):
        return ("<html>ok</html>", "http")

    def fake_extract(html, url, source="amazon", selectors=None, **kwargs):
        return [prod.Product(source=source, source_id="A1", title="Thing",
                             image_url="http://x/i.jpg", price_cents=999, currency="USD",
                             category=None, url=None)]

    monkeypatch.setattr(AmazonSearchPollable, "_render", fake_render)
    monkeypatch.setattr(prod, "extract_products", fake_extract)
    src = AmazonSearchPollable("ddr5 ram", source="newegg", category="RAM", max_results=5)
    res = await src.poll(None)
    assert res.ok
    assert res.payload[0]["source"] == "newegg"
    assert res.payload[0]["category"] == "RAM"      # category stamped onto results


# ── the shipped reference file is valid + complete ─────────────────────────────
def test_reference_profiles_shapes():
    p = load_profiles(path=EXAMPLE)
    assert p["ebay"]["engine"] == "browser" and ".s-item" in p["ebay"]["selectors"]["card"]
    assert p["walmart"]["selectors"]["id_attr"] == "data-item-id"
    for prof in p.values():
        assert "domain" in prof and "search_url" in prof


# ── Detail-page cache (_SeenStore) — generic engine, profile-agnostic ──────────
def test_seen_store_roundtrip_and_ttl(tmp_path):
    from ujin.poll.marketplace import _SeenStore

    clock = [1000.0]
    path = str(tmp_path / "seen.json")
    store = _SeenStore(path, ttl_secs=100, clock=lambda: clock[0])
    store.mark(["a", "b", None])                            # None is ignored
    store.save()

    clock[0] = 1050.0
    again = _SeenStore(path, ttl_secs=100, clock=lambda: clock[0])
    assert again.fresh_ids() == {"a", "b"}

    clock[0] = 1200.0
    expired = _SeenStore(path, ttl_secs=100, clock=lambda: clock[0])
    assert expired.fresh_ids() == set()
    assert expired.seen == {}                               # expired entries pruned


def test_detail_cache_off_by_default():
    src = MarketplaceSearchPollable(profile="newegg", profiles_path=str(EXAMPLE))
    assert src._seen is None


async def test_poll_passes_fresh_ids_to_children_and_marks_seen(monkeypatch, tmp_path):
    """poll() feeds cached ids into the child's skip set, and records new ids after the sweep."""
    from ujin.poll import amazon as amz
    from ujin.poll.base import PollResult

    captured_skip: list = []

    async def fake_child_poll(self, prev):
        captured_skip.append(set(self.skip_detail_ids))
        return PollResult(ok=True, changed=True, payload=[
            {"source": self.source, "source_id": "new-1", "title": "x", "price_cents": 100},
        ])

    monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_child_poll)
    path = str(tmp_path / "seen.json")
    src = MarketplaceSearchPollable(
        profile="newegg", profiles_path=str(EXAMPLE), terms_per_poll=1, seed=1,
        detail_cache=True, detail_cache_path=path,
    )
    src._seen.mark(["already-seen"])                        # pre-seed the cache
    res = await src.poll(None)
    assert res.ok
    assert captured_skip and "already-seen" in captured_skip[0]
    reloaded = type(src._seen)(path, ttl_secs=src._seen.ttl_secs)
    assert "new-1" in reloaded.fresh_ids()


def test_child_skip_detail_ids_defaults_to_empty_set():
    src = MarketplaceSearchPollable(profile="newegg", profiles_path=str(EXAMPLE), seed=1)
    child = src._child("ddr5 ram", "RAM")                   # no skip set passed
    assert child.skip_detail_ids == set()


# ── _load_file() coverage gaps ─────────────────────────────────────────────────

def test_load_file_not_found(tmp_path):
    """Non-existent path raises FileNotFoundError (line 53)."""
    from ujin.poll.marketplace import _load_file
    with pytest.raises(FileNotFoundError, match="not found"):
        _load_file(tmp_path / "no_such_file.yaml")


def test_load_file_json_branch(tmp_path):
    """JSON file is parsed via json.loads (line 56), then returned (line 62)."""
    from ujin.poll.marketplace import _load_file
    p = tmp_path / "profiles.json"
    p.write_text('{"shop": {"domain": "x.com", "search_url": "http://{domain}?q={query}"}}')
    data = _load_file(p)
    assert "shop" in data
    assert data["shop"]["domain"] == "x.com"


def test_load_file_non_dict_raises(tmp_path):
    """File whose root is not a mapping raises ValueError (line 61)."""
    from ujin.poll.marketplace import _load_file
    p = tmp_path / "bad.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="must be a mapping"):
        _load_file(p)


# ── _SeenStore.save() coverage gaps ───────────────────────────────────────────

def test_seen_store_save_no_parent_dir(monkeypatch, tmp_path):
    """path with no directory component skips makedirs (125->127)."""
    from ujin.poll.marketplace import _SeenStore
    monkeypatch.chdir(tmp_path)
    store = _SeenStore("bare_seen.json")
    store.mark(["id-bare"])
    store.save()
    assert (tmp_path / "bare_seen.json").exists()


def test_seen_store_save_oserror_swallowed(monkeypatch, tmp_path):
    """os.replace raises OSError -> error is logged, not re-raised (131-132)."""
    import ujin.poll.marketplace as mkt_mod
    from ujin.poll.marketplace import _SeenStore

    path = str(tmp_path / "seen.json")
    store = _SeenStore(path)
    store.mark(["id1"])

    def _raise(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(mkt_mod.os, "replace", _raise)
    store.save()   # must not raise


# ── _sample() empty categories ────────────────────────────────────────────────

_INLINE_EMPTY = {
    "empty": {
        "domain": "empty.example",
        "search_url": "https://{domain}/s?q={query}",
        "engine": "http",
        "keyterms": {},
    }
}


def test_sample_empty_categories_returns_empty():
    """_sample() returns [] immediately when no terms exist (line 216)."""
    src = MarketplaceSearchPollable(profile="empty", profiles=_INLINE_EMPTY, seed=1)
    assert src._sample() == []


# ── poll() skips exceptions and items without source_id ───────────────────────

async def test_poll_skips_exception_child(monkeypatch):
    """Child raising an exception is skipped; poll still returns ok (line 236)."""
    from ujin.poll import amazon as amz

    async def raises(self, prev):
        raise RuntimeError("boom")

    monkeypatch.setattr(amz.AmazonSearchPollable, "poll", raises)
    src = MarketplaceSearchPollable(profile="shop", profiles=INLINE, seed=1, terms_per_poll=1)
    r = await src.poll(None)
    assert r.ok is True
    assert r.payload == []


async def test_poll_item_without_source_id_included(monkeypatch):
    """Items with no source_id skip seen.add but are still appended (241->243)."""
    from ujin.poll import amazon as amz
    from ujin.poll.base import PollResult as _PR

    async def fake_poll(self, prev):
        return _PR(ok=True, changed=True, payload=[
            {"source_id": None, "title": "anonymous-item"},
        ])

    monkeypatch.setattr(amz.AmazonSearchPollable, "poll", fake_poll)
    src = MarketplaceSearchPollable(profile="shop", profiles=INLINE, seed=1, terms_per_poll=1)
    r = await src.poll(None)
    assert any(it["title"] == "anonymous-item" for it in r.payload)
