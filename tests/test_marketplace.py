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


async def test_poll_combines_and_dedupes(monkeypatch):
    """poll() orchestration without network: children are scraped, results deduped by id."""
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
    src = MarketplaceSearchPollable(profile="newegg", terms_per_poll=3, seed=1)
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


def test_unknown_profile_falls_back_to_amazon():
    src = MarketplaceSearchPollable(profile="nope")
    assert src.profile_name == "amazon"


# ── eBay / Walmart profiles ────────────────────────────────────────────────────
def test_ebay_profile_shape():
    prof = SITE_PROFILES["ebay"]
    assert prof["domain"] == "ebay.com"
    assert "{query}" in prof["search_url"] and "{domain}" in prof["search_url"]
    assert prof["engine"] == "browser"          # bare HTTP gets bounced to an error page
    sel = prof["selectors"]
    assert ".s-item" in sel["card"]
    assert prof["keyterms"]                       # has category banks


def test_walmart_profile_shape():
    prof = SITE_PROFILES["walmart"]
    assert prof["domain"] == "walmart.com"
    assert "/search?q={query}" in prof["search_url"]
    assert prof["engine"] == "browser"
    assert prof["selectors"]["id_attr"] == "data-item-id"
    assert prof["keyterms"]


def test_ebay_child_uses_profile_url_and_source():
    src = MarketplaceSearchPollable(profile="ebay", seed=1)
    child = src._child("wireless earbuds", "Electronics")
    assert child.source == "ebay"
    assert "ebay.com/sch/?_nkw=" in child.search_url     # /sch/i.html is blocked; /sch/ works
    assert "wireless+earbuds" in child.search_url       # query is quote_plus-encoded


def test_walmart_child_uses_profile_url_and_source():
    src = MarketplaceSearchPollable(profile="walmart", seed=1)
    child = src._child("olive oil", "Grocery")
    assert child.source == "walmart"
    assert "walmart.com/search?q=" in child.search_url
    assert "olive+oil" in child.search_url
