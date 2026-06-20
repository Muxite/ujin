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


# ── JSON-LD-first profiles (selectors: None ⇒ schema.org auto-extract) ──────────
_JSONLD_PROFILES = [
    "aliexpress", "target", "bestbuy", "etsy", "wayfair",
    "homedepot", "lowes", "bhphoto", "chewy", "ikea",
]


def test_all_new_jsonld_profiles_registered():
    for name in _JSONLD_PROFILES:
        assert name in SITE_PROFILES, name


def test_jsonld_profiles_have_no_selectors_and_browser_engine():
    """selectors=None routes through extract_products' JSON-LD/OpenGraph path (most reliable)."""
    for name in _JSONLD_PROFILES:
        prof = SITE_PROFILES[name]
        assert prof["selectors"] is None, name
        assert prof["engine"] == "browser", name          # SRPs are JS-rendered
        assert prof.get("wait_selector"), name             # browser needs something to wait on
        assert prof["keyterms"], name                      # has category banks


def test_jsonld_profile_search_url_templates_well_formed():
    for name in _JSONLD_PROFILES:
        url = SITE_PROFILES[name]["search_url"]
        assert "{query}" in url and "{domain}" in url, name
        assert url.startswith("https://"), name


def test_jsonld_child_uses_profile_url_source_and_no_selectors():
    src = MarketplaceSearchPollable(profile="target", seed=1)
    child = src._child("desk lamp", "Home")
    assert child.source == "target"
    assert "target.com/s?searchTerm=" in child.search_url
    assert "desk+lamp" in child.search_url
    assert child.selectors is None                          # JSON-LD path, not CSS cards


# ── Detail-page cache (_SeenStore) ─────────────────────────────────────────────
def test_seen_store_roundtrip_and_ttl(tmp_path):
    from ujin.poll.marketplace import _SeenStore

    clock = [1000.0]
    path = str(tmp_path / "seen.json")
    store = _SeenStore(path, ttl_secs=100, clock=lambda: clock[0])
    store.mark(["a", "b", None])                            # None is ignored
    store.save()

    # Reload before the TTL: both ids are still "fresh" (skip detail fetch).
    clock[0] = 1050.0
    again = _SeenStore(path, ttl_secs=100, clock=lambda: clock[0])
    assert again.fresh_ids() == {"a", "b"}

    # Past the TTL: pruned on load, nothing fresh.
    clock[0] = 1200.0
    expired = _SeenStore(path, ttl_secs=100, clock=lambda: clock[0])
    assert expired.fresh_ids() == set()
    assert expired.seen == {}                               # expired entries pruned


def test_detail_cache_off_by_default():
    src = MarketplaceSearchPollable(profile="target")
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
        profile="target", terms_per_poll=1, seed=1,
        detail_cache=True, detail_cache_path=path,
    )
    src._seen.mark(["already-seen"])                        # pre-seed the cache
    res = await src.poll(None)
    assert res.ok
    # The child received the pre-seeded id in its skip set (no detail re-fetch for it).
    assert captured_skip and "already-seen" in captured_skip[0]
    # The newly-surfaced id is now persisted for the next sweep.
    reloaded = type(src._seen)(path, ttl_secs=src._seen.ttl_secs)
    assert "new-1" in reloaded.fresh_ids()


def test_child_skip_detail_ids_defaults_to_empty_set():
    src = MarketplaceSearchPollable(profile="target", seed=1)
    child = src._child("desk lamp", "Home")               # no skip set passed
    assert child.skip_detail_ids == set()


# ── Google Shopping (best-effort CSS aggregator; no JSON-LD) ────────────────────
def test_google_shopping_profile_shape():
    prof = SITE_PROFILES["google_shopping"]
    # Unlike the JSON-LD stores this carries CSS selectors (the SERP has no structured data)
    # and routes through the browser engine; source_id is recovered from the offer link.
    assert prof["selectors"] is not None and prof["selectors"]["card"]
    assert prof["engine"] == "browser" and prof.get("wait_selector")
    assert "tbm=shop" in prof["search_url"] and "{query}" in prof["search_url"]
    assert prof["keyterms"]


def test_google_shopping_child_uses_profile_url_source_and_selectors():
    src = MarketplaceSearchPollable(profile="google_shopping", seed=1)
    child = src._child("wireless earbuds", "Electronics")
    assert child.source == "google_shopping"
    assert "google.com/search?tbm=shop" in child.search_url
    assert child.selectors is not None                     # CSS card path, not JSON-LD


def test_google_shopping_extracts_card_with_id_and_price():
    from ujin.extract.product import extract_products
    html = (
        '<div class="sh-dgr__content">'
        '<h3 class="tAxDx">Sony WH-1000XM5 Wireless Headphones</h3>'
        '<img class="ArOc1c" src="https://shopping.google/img/abc.jpg"/>'
        '<span class="a8Pemb">$348.00</span>'
        '<a class="shntl" href="https://www.google.com/shopping/product/1234567890?gl=us">x</a>'
        '</div>'
    )
    prods = extract_products(
        html, "https://www.google.com/", source="google_shopping",
        selectors=SITE_PROFILES["google_shopping"]["selectors"],
    )
    assert prods, "no products extracted"
    d = prods[0].__dict__
    assert d["source"] == "google_shopping"
    assert d["source_id"] == "1234567890"                  # /shopping/product/<id>
    assert d["price_cents"] == 34800                        # $348.00
    assert "Sony" in d["title"]
