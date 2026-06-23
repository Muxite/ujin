"""Offline coverage tests for ujin/poll/amazon.py.

Targets branches and lines not exercised by test_marketplace.py /
test_amazon_category.py:

  AmazonSearchPollable.poll():
    218-219  no HTML from any engine (early return)
    225->227 self.category is None (skip category stamp)
    227->224 self.clean_titles=False (skip title clean)
    230      with_description=True branch
    233      HTML returned but no products parsed

  _HarvestStore.save():
    339->341 parent dir is empty string (skip makedirs)
    345-346  OSError handler

  _HarvestStore.add_from_titles():
    360      pool size cap hit

  AmazonCategoryPollable.poll():
    480      exception result -> continue (skip)
    484      duplicate source_id -> continue (dedup)
    485->487 item with no source_id (skip seen.add)
    490->498 harvest=False (self._store is None)
"""
from __future__ import annotations

import pytest

from ujin.poll.base import PollResult
from ujin.poll.amazon import (
    AmazonSearchPollable,
    AmazonCategoryPollable,
    _HarvestStore,
)


# ── AmazonSearchPollable.poll() ────────────────────────────────────────────────

async def test_poll_no_html_returns_empty(monkeypatch):
    """_render returns no HTML -> ok=True, changed=False, empty payload (218-219)."""

    async def fake_render(self, url):
        return ("", "http")

    monkeypatch.setattr(AmazonSearchPollable, "_render", fake_render)
    src = AmazonSearchPollable("widget")
    r = await src.poll(None)
    assert r.ok is True
    assert r.changed is False
    assert r.fingerprint is None
    assert r.payload == []


async def test_poll_no_category_no_clean_titles(monkeypatch):
    """category=None skips category stamp (225->227); clean_titles=False skips
    title cleaning (227->224).  Both False branches run in a single test."""
    from ujin.extract import product as prod

    async def fake_render(self, url):
        return ("<html>ok</html>", "http")

    def fake_extract(html, url, source="amazon", selectors=None, **kw):
        return [prod.Product(source=source, source_id="P1",
                             title="  Raw Title  ",
                             image_url=None, price_cents=100,
                             currency="USD", category=None, url=None)]

    monkeypatch.setattr(AmazonSearchPollable, "_render", fake_render)
    monkeypatch.setattr(prod, "extract_products", fake_extract)

    src = AmazonSearchPollable("thing", category=None, clean_titles=False)
    r = await src.poll(None)
    assert r.ok is True
    # Title not cleaned (clean_titles=False)
    assert r.payload[0]["title"] == "  Raw Title  "
    # Category not stamped (category=None)
    assert r.payload[0]["category"] is None


async def test_poll_with_description_calls_attach(monkeypatch):
    """with_description=True + products -> _attach_descriptions is called (230)."""
    from ujin.extract import product as prod

    async def fake_render(self, url):
        return ("<html>ok</html>", "http")

    def fake_extract(html, url, source="amazon", selectors=None, **kw):
        return [prod.Product(source=source, source_id="P1",
                             title="Gadget", image_url=None,
                             price_cents=500, currency="USD",
                             category=None, url=None)]

    attach_calls: list[int] = []

    async def fake_attach(self, products):
        attach_calls.append(len(products))

    monkeypatch.setattr(AmazonSearchPollable, "_render", fake_render)
    monkeypatch.setattr(prod, "extract_products", fake_extract)
    monkeypatch.setattr(AmazonSearchPollable, "_attach_descriptions", fake_attach)

    src = AmazonSearchPollable("thing", with_description=True)
    r = await src.poll(None)
    assert r.ok is True
    assert attach_calls == [1]   # _attach_descriptions was invoked


async def test_poll_html_but_no_products_warns(monkeypatch):
    """HTML fetched but extract_products returns [] -> warning logged, empty
    payload returned (233)."""
    from ujin.extract import product as prod

    async def fake_render(self, url):
        return ("<html>no products here</html>", "http")

    def fake_extract(html, url, source="amazon", selectors=None, **kw):
        return []

    monkeypatch.setattr(AmazonSearchPollable, "_render", fake_render)
    monkeypatch.setattr(prod, "extract_products", fake_extract)

    src = AmazonSearchPollable("thing")
    r = await src.poll(None)
    assert r.ok is True
    assert r.payload == []


# ── _HarvestStore.save() ──────────────────────────────────────────────────────

def test_harvest_store_save_no_parent_dir(monkeypatch, tmp_path):
    """path has no directory component -> makedirs is skipped (339->341)."""
    monkeypatch.chdir(tmp_path)
    store = _HarvestStore("bare_harvest.json")
    store.pool["word"] = "Electronics"
    store.save()
    assert (tmp_path / "bare_harvest.json").exists()


def test_harvest_store_save_oserror_swallowed(monkeypatch, tmp_path):
    """os.replace raises OSError -> error is logged, not re-raised (345-346)."""
    import ujin.poll.amazon as amz_mod

    path = str(tmp_path / "h.json")
    store = _HarvestStore(path)
    store.pool["word"] = "Kitchen"

    def _raise(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(amz_mod.os, "replace", _raise)
    store.save()   # must not raise


# ── _HarvestStore.add_from_titles() ──────────────────────────────────────────

def test_harvest_add_titles_pool_cap(tmp_path):
    """When pool reaches max_pool, add_from_titles stops and returns (360)."""
    store = _HarvestStore(str(tmp_path / "h.json"), max_pool=2)
    items = [{"title": "bamboo stainless carbon blades", "category": "Kitchen"}]
    added = store.add_from_titles(items, min_len=4)
    # With max_pool=2 the pool cannot grow past 2 entries.
    assert len(store.pool) <= 2
    # Some words were harvested before the cap was hit.
    assert added >= 1


# ── AmazonCategoryPollable.poll() ─────────────────────────────────────────────

async def test_category_poll_skips_exception_child(monkeypatch):
    """A child that raises contributes nothing; poll still returns ok (480)."""
    import ujin.poll.amazon as amz_mod

    calls: list[int] = [0]

    async def sometimes_raises(self, prev):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("network error")
        return PollResult(ok=True, changed=True,
                          fingerprint="fp", payload=[{"source_id": "ok-1"}])

    monkeypatch.setattr(amz_mod.AmazonSearchPollable, "poll", sometimes_raises)
    src = AmazonCategoryPollable(categories=["Kitchen"], terms_per_poll=2, seed=1)
    r = await src.poll(None)
    assert r.ok is True
    # Only the non-exception child's item is present.
    ids = [it.get("source_id") for it in r.payload]
    assert "ok-1" in ids


async def test_category_poll_dedupes_source_ids(monkeypatch):
    """Items with duplicate source_id within one poll are collapsed (484)."""
    import ujin.poll.amazon as amz_mod

    async def fake_poll(self, prev):
        return PollResult(ok=True, changed=True, payload=[
            {"source_id": "dup", "title": "First"},
            {"source_id": "dup", "title": "Second"},   # duplicate
        ])

    monkeypatch.setattr(amz_mod.AmazonSearchPollable, "poll", fake_poll)
    src = AmazonCategoryPollable(categories=["Kitchen"], terms_per_poll=1, seed=1)
    r = await src.poll(None)
    ids = [it["source_id"] for it in r.payload]
    assert ids.count("dup") == 1   # only one entry survives


async def test_category_poll_item_with_no_source_id(monkeypatch):
    """Items with no source_id are included without tracking (485->487)."""
    import ujin.poll.amazon as amz_mod

    async def fake_poll(self, prev):
        return PollResult(ok=True, changed=True, payload=[
            {"source_id": None, "title": "untracked-item"},
        ])

    monkeypatch.setattr(amz_mod.AmazonSearchPollable, "poll", fake_poll)
    src = AmazonCategoryPollable(categories=["Kitchen"], terms_per_poll=1, seed=1)
    r = await src.poll(None)
    assert any(it["title"] == "untracked-item" for it in r.payload)


async def test_category_poll_no_harvest_store(monkeypatch):
    """harvest=False means self._store is None -> harvest block is skipped (490->498)."""
    import ujin.poll.amazon as amz_mod

    async def fake_poll(self, prev):
        return PollResult(ok=True, changed=True, payload=[
            {"source_id": "X1", "title": "Widget"},
        ])

    monkeypatch.setattr(amz_mod.AmazonSearchPollable, "poll", fake_poll)
    src = AmazonCategoryPollable(
        categories=["Kitchen"], terms_per_poll=1, seed=1, harvest=False
    )
    assert src._store is None
    r = await src.poll(None)
    assert r.ok is True
    assert r.payload[0]["source_id"] == "X1"
