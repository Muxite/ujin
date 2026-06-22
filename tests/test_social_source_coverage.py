"""Branch-coverage tests for the four social source modules.

All tests are fully offline — no real network calls, DNS lookups, or
aiohttp connections. Mocked sessions / patched callables only.

Covers the remaining branches in _syndication.py not reached by
test_cov_gap_fill.py:
  - syndication text response parses to a non-dict JSON value (list/number)
    → falls through to _from_html instead of _from_json  (branch 82->86)
  - _from_json: out is empty and body is not a string  (branch 114->116)
  - _from_html: valid permalink but whitespace-only text → continue  (line 134)
  - _from_html: count ceiling reached → break  (line 137)
"""
from __future__ import annotations

import json as _json

import pytest

from ujin.sources.social._syndication import (
    _from_html,
    _from_json,
    syndication_posts,
)
from ujin.sources.social.twitter import SocialPost


# --------------------------------------------------------------------------- #
# Minimal fakes (same shape as test_cov_gap_fill.py helpers but independent)
# --------------------------------------------------------------------------- #

class _SResp:
    """Minimal async-CM response stub for syndication_posts tests."""

    def __init__(self, status: int, body, content_type: str = "text/html"):
        self.status = status
        self._body = body
        self.headers = {"content-type": content_type}

    async def json(self, *, content_type=None):
        return self._body

    async def text(self):
        return self._body if isinstance(self._body, str) else _json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _SSess:
    """Minimal async-CM session stub."""

    def __init__(self, resp: _SResp):
        self._resp = resp

    def get(self, *a, **kw):
        return self._resp

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


# --------------------------------------------------------------------------- #
# _syndication.py — branch 82->86
# json.loads(text) succeeds but returns a non-dict (e.g. a list)
# → isinstance check is False → falls to _from_html
# --------------------------------------------------------------------------- #

async def test_syndication_text_json_non_dict_falls_to_html():
    """text/html body that is a valid JSON array (not dict) falls to _from_html."""
    # Passing a JSON-serialised list so json.loads succeeds, but the result
    # is not a dict; the isinstance(data, dict) branch is False (82->86).
    body = _json.dumps([{"type": "tweet", "content": "ignored"}])
    sess = _SSess(_SResp(200, body, "text/html"))
    posts = await syndication_posts("user", count=5, session=sess)
    # _from_html on a JSON array string finds no tweet elements → []
    assert posts == []


async def test_syndication_text_json_number_falls_to_html():
    """text/html body that is a valid JSON number also falls to _from_html."""
    body = "42"  # valid JSON but not a dict
    sess = _SSess(_SResp(200, body, "text/html"))
    posts = await syndication_posts("user", count=5, session=sess)
    assert posts == []


# --------------------------------------------------------------------------- #
# _from_json — branch 114->116
# out is empty AND body is not a string → skip HTML fallback, return out
# --------------------------------------------------------------------------- #

def test_from_json_body_none_returns_empty():
    """No entries + body=None: non-string body skips HTML fallback (114->116)."""
    posts = _from_json({"timeline": {"entries": []}, "body": None}, count=5)
    assert posts == []


def test_from_json_body_int_returns_empty():
    """No entries + body=0 (non-string): branch 114->116 is taken."""
    posts = _from_json({"body": 0}, count=5)
    assert posts == []


def test_from_json_no_body_key_returns_empty():
    """No entries and no body key at all → out is empty, return []."""
    posts = _from_json({}, count=5)
    assert posts == []


# --------------------------------------------------------------------------- #
# _from_html — line 134
# Tweet node has a valid permalink but text content is whitespace-only
# → stripped to "" → continue without appending
# --------------------------------------------------------------------------- #

def test_from_html_skips_whitespace_only_text():
    """Tweet with matching href but empty text after strip is skipped (line 134)."""
    html = (
        '<div class="timeline-Tweet">'
        '<p class="timeline-Tweet-text">   </p>'
        '<a href="https://twitter.com/user/status/111">link</a>'
        "</div>"
        '<div class="timeline-Tweet">'
        '<p class="timeline-Tweet-text">real text</p>'
        '<a href="https://twitter.com/user/status/222">link</a>'
        "</div>"
    )
    posts = _from_html(html, count=10)
    # First tweet dropped (empty text); second kept.
    assert len(posts) == 1
    assert posts[0].url == "https://twitter.com/user/status/222"
    assert posts[0].text == "real text"


def test_from_html_all_whitespace_text_returns_empty():
    """Only tweet has whitespace text → _from_html returns []."""
    html = (
        '<div class="timeline-Tweet">'
        '<p class="timeline-Tweet-text">\n\t</p>'
        '<a href="https://twitter.com/user/status/123">link</a>'
        "</div>"
    )
    posts = _from_html(html, count=5)
    assert posts == []


# --------------------------------------------------------------------------- #
# _from_html — line 137
# count ceiling reached → break exits the loop early
# --------------------------------------------------------------------------- #

def _make_tweet_html(n: int) -> str:
    return "".join(
        f'<div class="timeline-Tweet">'
        f'<p class="timeline-Tweet-text">post {i}</p>'
        f'<a href="https://twitter.com/u/status/{i}">link</a>'
        f"</div>"
        for i in range(n)
    )


def test_from_html_count_limit_stops_early():
    """_from_html stops appending once count is hit (line 137 break)."""
    html = _make_tweet_html(10)
    posts = _from_html(html, count=3)
    assert len(posts) == 3
    # Verify the first three, not later ones
    assert posts[0].url == "https://twitter.com/u/status/0"
    assert posts[2].url == "https://twitter.com/u/status/2"


def test_from_html_count_one():
    """count=1 produces exactly one post then breaks."""
    html = _make_tweet_html(5)
    posts = _from_html(html, count=1)
    assert len(posts) == 1
    assert posts[0].url == "https://twitter.com/u/status/0"


def test_from_html_count_exceeds_available():
    """When count > available tweets, all are returned (no off-by-one)."""
    html = _make_tweet_html(3)
    posts = _from_html(html, count=100)
    assert len(posts) == 3
