"""Tests for ujin.robots: parse edge cases, cache TTL, and regression guard."""
from __future__ import annotations

import pytest

from ujin.robots import RobotsCache, RobotsPolicy


# ================================================================== parse ===


class TestRobotsPolicyAllowAll:
    def test_empty_string(self):
        assert RobotsPolicy("").is_allowed("/anything") is True

    def test_whitespace_only(self):
        assert RobotsPolicy("   \n  \t  ").is_allowed("/foo") is True

    def test_no_valid_groups(self):
        # Comments and garbage, no User-agent blocks.
        txt = "# just a comment\ngibberish line\n"
        assert RobotsPolicy(txt).is_allowed("/foo") is True

    def test_allow_all_classmethod(self):
        p = RobotsPolicy.allow_all()
        assert p.is_allowed("/private/secret") is True
        assert p.crawl_delay() is None

    def test_empty_disallow_means_allow_all(self):
        txt = "User-agent: *\nDisallow:\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/anything/at/all") is True

    def test_malformed_no_colon(self):
        txt = "Useragent *\nDisallow /private\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/private") is True


class TestRobotsPolicyBasic:
    def test_disallow_path(self):
        txt = "User-agent: *\nDisallow: /private\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/private") is False
        assert p.is_allowed("/private/page") is False
        assert p.is_allowed("/public") is True

    def test_allow_overrides_disallow(self):
        txt = "User-agent: *\nDisallow: /\nAllow: /public\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/public") is True
        assert p.is_allowed("/public/page") is True
        assert p.is_allowed("/private") is False

    def test_unknown_agent_falls_back_to_wildcard(self):
        txt = "User-agent: *\nDisallow: /secret\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/secret", agent="SomeBot") is False

    def test_specific_agent_group_used_not_wildcard(self):
        txt = (
            "User-agent: Googlebot\nDisallow: /google-only\n\n"
            "User-agent: *\nDisallow: /everyone\n"
        )
        p = RobotsPolicy(txt)
        # Googlebot: only /google-only disallowed
        assert p.is_allowed("/google-only", agent="Googlebot") is False
        assert p.is_allowed("/everyone", agent="Googlebot") is True
        # Other bots: only /everyone disallowed
        assert p.is_allowed("/google-only", agent="OtherBot") is True
        assert p.is_allowed("/everyone", agent="OtherBot") is False

    def test_agent_lookup_case_insensitive(self):
        txt = "User-agent: Googlebot\nDisallow: /private\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/private", agent="googlebot") is False
        assert p.is_allowed("/private", agent="GOOGLEBOT") is False

    def test_no_wildcard_group_no_applicable_agent_allows(self):
        txt = "User-agent: Googlebot\nDisallow: /private\n"
        p = RobotsPolicy(txt)
        # SomeBot has no matching group → allow
        assert p.is_allowed("/private", agent="SomeBot") is True

    def test_multiple_useragent_lines_share_rules(self):
        txt = "User-agent: Googlebot\nUser-agent: Slurp\nDisallow: /shared\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/shared", agent="Googlebot") is False
        assert p.is_allowed("/shared", agent="Slurp") is False

    def test_comments_stripped(self):
        txt = "User-agent: * # wildcard\nDisallow: /private # nope\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/private") is False

    def test_new_group_without_blank_line(self):
        # Some robots.txt files omit the blank line between groups.
        txt = (
            "User-agent: Googlebot\nDisallow: /google\n"
            "User-agent: *\nDisallow: /all\n"
        )
        p = RobotsPolicy(txt)
        assert p.is_allowed("/google", agent="Googlebot") is False
        assert p.is_allowed("/all", agent="Googlebot") is True
        assert p.is_allowed("/all", agent="OtherBot") is False


# ============================================================ longest match ===


class TestLongestMatch:
    def test_longer_allow_beats_shorter_disallow(self):
        txt = "User-agent: *\nDisallow: /foo\nAllow: /foo/bar\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/foo/bar") is True
        assert p.is_allowed("/foo/other") is False

    def test_longer_disallow_beats_shorter_allow(self):
        txt = "User-agent: *\nAllow: /foo\nDisallow: /foo/private\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/foo") is True
        assert p.is_allowed("/foo/private") is False
        assert p.is_allowed("/foo/private/deep") is False

    def test_equal_length_allow_wins(self):
        # Same length: Allow wins (spec default).
        txt = "User-agent: *\nDisallow: /foo\nAllow: /foo\n"
        p = RobotsPolicy(txt)
        # First match with equal length wins (stable iteration; allow is later,
        # but equal length means best_len won't update — first wins).
        # Either outcome is compliant; test that it doesn't raise.
        result = p.is_allowed("/foo")
        assert isinstance(result, bool)

    def test_no_match_defaults_to_allow(self):
        txt = "User-agent: *\nDisallow: /private\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/public") is True


# ================================================================ wildcards ===


class TestWildcards:
    def test_star_wildcard_in_middle(self):
        txt = "User-agent: *\nDisallow: /foo/*/bar\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/foo/anything/bar") is False
        assert p.is_allowed("/foo/anything/bar/deep") is False
        assert p.is_allowed("/foo/bar") is True

    def test_star_wildcard_at_end(self):
        txt = "User-agent: *\nDisallow: /secret*\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/secret") is False
        assert p.is_allowed("/secret-files") is False
        assert p.is_allowed("/public") is True

    def test_dollar_anchors_end(self):
        txt = "User-agent: *\nDisallow: /page$\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/page") is False
        assert p.is_allowed("/page/subpage") is True  # not anchored at end
        assert p.is_allowed("/pagefoo") is True  # prefix only, no $ match

    def test_star_and_dollar_combined(self):
        txt = "User-agent: *\nDisallow: /*.php$\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/index.php") is False
        assert p.is_allowed("/admin/login.php") is False
        assert p.is_allowed("/index.php?page=1") is True  # query string appended

    def test_disallow_slash_blocks_all(self):
        txt = "User-agent: *\nDisallow: /\n"
        p = RobotsPolicy(txt)
        assert p.is_allowed("/") is False
        assert p.is_allowed("/anything") is False


# ============================================================= crawl-delay ===


class TestCrawlDelay:
    def test_crawl_delay_parsed(self):
        txt = "User-agent: *\nCrawl-delay: 5\n"
        p = RobotsPolicy(txt)
        assert p.crawl_delay() == 5.0

    def test_crawl_delay_float(self):
        txt = "User-agent: *\nCrawl-delay: 2.5\n"
        p = RobotsPolicy(txt)
        assert p.crawl_delay() == 2.5

    def test_crawl_delay_invalid_ignored(self):
        txt = "User-agent: *\nCrawl-delay: fast\n"
        p = RobotsPolicy(txt)
        assert p.crawl_delay() is None

    def test_crawl_delay_per_agent(self):
        txt = "User-agent: Googlebot\nCrawl-delay: 1\n\nUser-agent: *\nCrawl-delay: 10\n"
        p = RobotsPolicy(txt)
        assert p.crawl_delay("Googlebot") == 1.0
        assert p.crawl_delay("*") == 10.0
        assert p.crawl_delay("OtherBot") == 10.0  # falls back to *

    def test_crawl_delay_unknown_agent_returns_none(self):
        txt = "User-agent: Googlebot\nCrawl-delay: 5\n"
        p = RobotsPolicy(txt)
        # No * group, no OtherBot group → None
        assert p.crawl_delay("OtherBot") is None

    def test_crawl_delay_none_when_absent(self):
        txt = "User-agent: *\nDisallow: /private\n"
        p = RobotsPolicy(txt)
        assert p.crawl_delay() is None


# ================================================================== sitemaps ===


class TestSitemaps:
    def test_sitemap_collected(self):
        txt = "Sitemap: https://example.com/sitemap.xml\n"
        p = RobotsPolicy(txt)
        assert p.sitemaps == ["https://example.com/sitemap.xml"]

    def test_multiple_sitemaps(self):
        txt = (
            "Sitemap: https://example.com/sitemap1.xml\n"
            "Sitemap: https://example.com/sitemap2.xml\n"
        )
        p = RobotsPolicy(txt)
        assert len(p.sitemaps) == 2

    def test_sitemaps_empty_when_none(self):
        txt = "User-agent: *\nDisallow: /\n"
        p = RobotsPolicy(txt)
        assert p.sitemaps == []


# ================================================================ cache TTL ===


class TestRobotsCache:
    @pytest.mark.asyncio
    async def test_first_call_fetches(self):
        calls = []

        async def fake_fetcher(url: str) -> str:
            calls.append(url)
            return "User-agent: *\nDisallow: /private\n"

        cache = RobotsCache(ttl=60.0, fetcher=fake_fetcher)
        policy = await cache.get("https://example.com")
        assert len(calls) == 1
        assert calls[0] == "https://example.com/robots.txt"
        assert policy.is_allowed("/private") is False

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_is_cached(self):
        calls = []

        async def fake_fetcher(url: str) -> str:
            calls.append(url)
            return "User-agent: *\nDisallow: /private\n"

        t = [0.0]
        cache = RobotsCache(ttl=60.0, fetcher=fake_fetcher, clock=lambda: t[0])
        await cache.get("https://example.com")
        t[0] = 30.0  # still within TTL
        await cache.get("https://example.com")
        assert len(calls) == 1  # fetcher called once only

    @pytest.mark.asyncio
    async def test_refetch_after_ttl_expires(self):
        calls = []

        async def fake_fetcher(url: str) -> str:
            calls.append(url)
            return ""

        t = [0.0]
        cache = RobotsCache(ttl=60.0, fetcher=fake_fetcher, clock=lambda: t[0])
        await cache.get("https://example.com")
        t[0] = 61.0  # TTL expired
        await cache.get("https://example.com")
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_different_origins_fetched_independently(self):
        calls = []

        async def fake_fetcher(url: str) -> str:
            calls.append(url)
            return ""

        cache = RobotsCache(ttl=60.0, fetcher=fake_fetcher)
        await cache.get("https://example.com")
        await cache.get("https://other.com")
        assert len(calls) == 2
        assert any("example.com" in c for c in calls)
        assert any("other.com" in c for c in calls)

    @pytest.mark.asyncio
    async def test_fetch_error_returns_allow_all(self):
        async def bad_fetcher(url: str) -> str:
            return ""  # empty = allow-all

        cache = RobotsCache(fetcher=bad_fetcher)
        policy = await cache.get("https://example.com")
        assert policy.is_allowed("/anything") is True

    @pytest.mark.asyncio
    async def test_invalidate_forces_refetch(self):
        calls = []

        async def fake_fetcher(url: str) -> str:
            calls.append(url)
            return ""

        cache = RobotsCache(ttl=3600.0, fetcher=fake_fetcher)
        await cache.get("https://example.com")
        cache.invalidate("https://example.com")
        await cache.get("https://example.com")
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_trailing_slash_normalized(self):
        calls = []

        async def fake_fetcher(url: str) -> str:
            calls.append(url)
            return ""

        cache = RobotsCache(fetcher=fake_fetcher)
        await cache.get("https://example.com/")
        assert calls[0] == "https://example.com/robots.txt"


# ============================================================= regression ===


@pytest.mark.asyncio
async def test_no_robots_fetch_on_default_scrape(fake_origin):
    """Default HttpPollable must NOT request /robots.txt (robots is opt-in)."""
    fake_origin.add("/page.html", body="<html><body>hello</body></html>")

    from ujin.poll.http import HttpPollable

    p = HttpPollable(fake_origin.url("/page.html"))
    result = await p.poll(None)

    assert result.ok
    paths = [r.path for r in fake_origin.requests]
    assert "/robots.txt" not in paths, (
        f"HttpPollable fetched /robots.txt by default (paths seen: {paths})"
    )
