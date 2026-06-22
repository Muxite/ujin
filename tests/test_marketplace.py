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

    def fake_extract(html, url, source="amazon", selectors=None):
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
