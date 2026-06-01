"""Social source tests: syndication parsing + brave gate.

Pure-parse paths are tested directly; the network-bound chain is exercised by
monkeypatching the leg functions.
"""
from __future__ import annotations

import pytest

from ujin.sources.social import BraveNotConfigured, twitter_search, x_posts
from ujin.sources.social._syndication import _from_html, _from_json

# asyncio_mode=auto (pyproject) runs async tests without explicit marks.


def test_syndication_from_json_structured():
    data = {
        "timeline": {
            "entries": [
                {
                    "type": "tweet",
                    "content": {"tweet": {
                        "full_text": "Hello world from the timeline",
                        "permalink_url": "https://x.com/user/status/123",
                    }},
                },
                {"type": "other", "content": {}},
            ]
        }
    }
    posts = _from_json(data, count=10)
    assert len(posts) == 1
    assert posts[0].url == "https://x.com/user/status/123"
    assert "Hello world" in posts[0].text


def test_syndication_from_html_fallback():
    html = """
    <div class="timeline-Tweet">
      <p class="timeline-Tweet-text">An embedded tweet body</p>
      <a href="https://twitter.com/user/status/456">link</a>
    </div>
    """
    posts = _from_html(html, count=10)
    assert len(posts) == 1
    assert posts[0].url == "https://twitter.com/user/status/456"


async def test_twitter_search_requires_key():
    with pytest.raises(BraveNotConfigured):
        await twitter_search("someuser", 5, api_key="")


async def test_x_chain_syndication_leg(monkeypatch):
    """No nitter + brave disabled → syndication leg answers."""
    from ujin.sources.social import _syndication
    from ujin.sources.social.twitter import SocialPost

    async def fake_syndication(username, count=20, *, session=None):
        return [SocialPost(url="https://x.com/u/status/1", text="hi")]

    # x.py imported syndication_posts by name; patch there.
    import ujin.sources.social.x as xmod

    monkeypatch.setattr(xmod, "syndication_posts", fake_syndication)
    result = await x_posts("u", 5, nitter=None, allow_brave=False)
    assert result.leg == "syndication"
    assert len(result.posts) == 1


async def test_x_chain_empty_when_all_fail(monkeypatch):
    import ujin.sources.social.x as xmod
    from ujin.sources.social.twitter import SocialPost  # noqa: F401

    async def empty_syndication(username, count=20, *, session=None):
        return []

    monkeypatch.setattr(xmod, "syndication_posts", empty_syndication)
    result = await x_posts("u", 5, nitter=None, allow_brave=False)
    assert result.leg == "empty"
    assert result.posts == []
