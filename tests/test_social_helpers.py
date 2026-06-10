"""Social source helpers that run offline: truth RSS mapping, brave key
resolution, mastodon account parsing/HTML stripping."""
from __future__ import annotations

import pytest

from ujin.sources.social.mastodon import _resolve_ua, _split_account, _strip_html
from ujin.sources.social.truth import truth_social_posts
from ujin.sources.social.twitter import (
    BraveNotConfigured,
    _resolve_key,
    twitter_search,
)


# ── truth ────────────────────────────────────────────────────────────────────

async def test_truth_posts_map_feed_items(monkeypatch):
    class _Item:
        def __init__(self, url, title, summary=""):
            self.url, self.title, self.summary = url, title, summary
            self.published = None

    captured = {}

    async def fake_parse(url):
        captured["url"] = url
        return [_Item("https://truthsocial.com/@u/1", "first post", "with body"),
                _Item("https://truthsocial.com/@u/2", "same", "same"),
                _Item("https://truthsocial.com/@u/3", "third post")]

    monkeypatch.setattr("ujin.sources.social.truth.parse_feed", fake_parse)
    posts = await truth_social_posts("@theuser", count=2)
    assert captured["url"] == "https://truthsocial.com/@theuser/feed.rss"
    assert len(posts) == 2
    assert posts[0].text == "first post with body"   # title + distinct summary
    assert posts[1].text == "same"                   # identical summary not doubled


async def test_truth_count_floor_is_one(monkeypatch):
    async def fake_parse(url):
        class _I:
            url, title, summary, published = "https://t/1", "t", "", None
        return [_I(), _I()]

    monkeypatch.setattr("ujin.sources.social.truth.parse_feed", fake_parse)
    assert len(await truth_social_posts("u", count=0)) == 1


# ── twitter / brave ──────────────────────────────────────────────────────────

def test_resolve_key_precedence(monkeypatch):
    monkeypatch.setenv("SEARCH_API_KEY", "from-env")
    assert _resolve_key("explicit") == "explicit"
    assert _resolve_key(None) == "from-env"
    monkeypatch.delenv("SEARCH_API_KEY")
    assert _resolve_key(None) == ""


async def test_twitter_search_without_key_raises(monkeypatch):
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    with pytest.raises(BraveNotConfigured):
        await twitter_search("someone")


# ── mastodon helpers ─────────────────────────────────────────────────────────

def test_split_account_forms():
    a = _split_account("@user@Mastodon.Example")
    assert a.user == "user" and a.instance == "mastodon.example"
    b = _split_account("user@mastodon.example")
    assert b.user == "user" and b.instance == "mastodon.example"


def test_split_account_invalid_raises():
    with pytest.raises(ValueError):
        _split_account("justauser")
    with pytest.raises(ValueError):
        _split_account("@user@")


def test_strip_html():
    assert _strip_html("<p>hello <b>world</b></p>") == "hello world"
    assert _strip_html("") == ""


def test_resolve_ua_default_and_override():
    assert _resolve_ua(None)
    assert _resolve_ua("custom/1.0") == "custom/1.0"
