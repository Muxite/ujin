"""robots.txt policy: parse, cache, and query per-agent Allow/Disallow rules.

Usage (parse only — no I/O):
    policy = RobotsPolicy(robots_txt_text)
    policy.is_allowed("/private/page", agent="Googlebot")  # -> bool
    policy.crawl_delay("Googlebot")                        # -> float | None

Usage (fetch + TTL cache):
    cache = RobotsCache(ttl=3600)
    policy = await cache.get("https://example.com")
    policy.is_allowed("/path")
"""
from __future__ import annotations

import re
import time
from typing import Awaitable, Callable, Optional


class RobotsPolicy:
    """Parsed robots.txt — pure query over already-parsed text; no I/O."""

    def __init__(self, text: str = "") -> None:
        # agent (lower) -> list of (allow: bool, pattern: str)
        self._rules: dict[str, list[tuple[bool, str]]] = {}
        self._delays: dict[str, float] = {}
        self._sitemaps: list[str] = []
        if text and text.strip():
            self._parse(text)

    # ------------------------------------------------------------------ parse

    def _parse(self, text: str) -> None:
        current_agents: list[str] = []
        in_directives = False  # True once Allow/Disallow/Crawl-delay seen

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                current_agents = []
                in_directives = False
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()

            if key == "user-agent":
                if in_directives:
                    # New group starts without a blank-line separator.
                    current_agents = []
                    in_directives = False
                agent = val.lower()
                if agent not in self._rules:
                    self._rules[agent] = []
                current_agents.append(agent)
            elif key == "allow" and current_agents:
                in_directives = True
                for a in current_agents:
                    self._rules[a].append((True, val))
            elif key == "disallow" and current_agents:
                in_directives = True
                if val:  # empty Disallow means "allow all" — omit the rule
                    for a in current_agents:
                        self._rules[a].append((False, val))
            elif key == "crawl-delay" and current_agents:
                in_directives = True
                try:
                    delay = float(val)
                    for a in current_agents:
                        self._delays[a] = delay
                except ValueError:
                    pass
            elif key == "sitemap":
                self._sitemaps.append(val)

    # ------------------------------------------------------------------ query

    def is_allowed(self, path: str, agent: str = "*") -> bool:
        """Return True if *agent* may fetch *path*.

        Falls back to the ``*`` group when no agent-specific group exists.
        Empty/missing/malformed robots.txt returns True (allow-all).
        """
        agent_key = agent.lower()
        if agent_key in self._rules:
            rules = self._rules[agent_key]
        elif "*" in self._rules:
            rules = self._rules["*"]
        else:
            return True  # no applicable group → allow

        if not rules:
            return True

        # Longest-match (by pattern length) wins; tie = allow wins per spec.
        best_len = -1
        best_allow = True

        for allow, pattern in rules:
            matched, length = self._match(pattern, path)
            if matched and length > best_len:
                best_len = length
                best_allow = allow

        return best_allow

    def crawl_delay(self, agent: str = "*") -> Optional[float]:
        """Return the Crawl-delay for *agent*, falling back to ``*``, else None."""
        a = agent.lower()
        if a in self._delays:
            return self._delays[a]
        return self._delays.get("*")

    @property
    def sitemaps(self) -> list[str]:
        """Sitemap URLs declared in this robots.txt."""
        return list(self._sitemaps)

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _match(pattern: str, path: str) -> tuple[bool, int]:
        """Return (matched, effective_length) for longest-match precedence.

        Pattern length is used so ``/foo/bar`` beats ``/foo`` on ``/foo/bar/x``.
        An empty pattern matches everything with length 0 (empty Disallow = allow-all).
        """
        if not pattern:
            return True, 0

        anchored = pattern.endswith("$")
        core = pattern[:-1] if anchored else pattern

        # * -> .* (greedy); escape everything else
        regex = ".*".join(re.escape(part) for part in core.split("*"))
        if anchored:
            regex += "$"

        try:
            if re.match(regex, path):
                return True, len(pattern)
        except re.error:
            pass
        return False, 0

    @classmethod
    def allow_all(cls) -> "RobotsPolicy":
        """Convenience: return a policy that allows everything."""
        return cls("")


# ============================================================= fetch+cache ===

_DEFAULT_TTL = 3600.0  # 1 hour


async def _http_fetcher(url: str) -> str:
    """Fetch *url* and return body text; returns '' on any error."""
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.text(errors="replace")
    except Exception:
        pass
    return ""


class RobotsCache:
    """Fetch-and-cache robots.txt per origin with a configurable TTL.

    Opt-in only — nothing in the default scrape/poll path instantiates this.
    Crawl-delay values surfaced here are intended as future inputs to the
    learned-rate-limit system (ujin.adapt.concurrency).

    Args:
        ttl: Seconds before a cached policy is re-fetched. Default 1 hour.
        fetcher: ``async (url: str) -> str`` — injectable for tests.
        clock: ``() -> float`` — injectable wall clock (seconds). Default
               ``time.monotonic``.
    """

    def __init__(
        self,
        ttl: float = _DEFAULT_TTL,
        fetcher: Optional[Callable[[str], Awaitable[str]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._ttl = ttl
        self._fetcher = fetcher or _http_fetcher
        self._clock = clock or time.monotonic
        # base_url -> (fetched_at, policy)
        self._store: dict[str, tuple[float, RobotsPolicy]] = {}

    async def get(self, base_url: str) -> RobotsPolicy:
        """Return the cached or freshly-fetched RobotsPolicy for *base_url*.

        *base_url* should be the scheme + host (e.g. ``https://example.com``).
        Missing or unreachable robots.txt returns an allow-all policy.
        """
        now = self._clock()
        entry = self._store.get(base_url)
        if entry is not None:
            fetched_at, policy = entry
            if now - fetched_at < self._ttl:
                return policy

        robots_url = base_url.rstrip("/") + "/robots.txt"
        text = await self._fetcher(robots_url)
        policy = RobotsPolicy(text)
        self._store[base_url] = (now, policy)
        return policy

    def invalidate(self, base_url: str) -> None:
        """Evict *base_url* from the cache, forcing a fresh fetch next call."""
        self._store.pop(base_url, None)
