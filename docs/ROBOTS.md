# robots.txt Policy (`ujin.robots`)

Parse and query robots.txt rules without any I/O, plus an optional TTL fetch+cache layer.

## Quick start

```python
from ujin.robots import RobotsPolicy, RobotsCache

# --- Parse only (no I/O) ---
policy = RobotsPolicy(robots_txt_text)
policy.is_allowed("/private/page")            # True or False (agent='*')
policy.is_allowed("/page", agent="Googlebot") # agent-specific check
policy.crawl_delay("Googlebot")              # float | None
policy.sitemaps                              # list[str]

# --- Fetch + TTL cache ---
cache = RobotsCache(ttl=3600)
policy = await cache.get("https://example.com")
policy.is_allowed("/path")
```

## `RobotsPolicy`

`RobotsPolicy(text: str = "")` — pure parser, no network calls.

| Method | Signature | Notes |
|--------|-----------|-------|
| `is_allowed` | `(path, agent='*') -> bool` | Longest-match precedence; falls back to `*` group |
| `crawl_delay` | `(agent='*') -> float \| None` | Falls back to `*` group; `None` if absent |
| `sitemaps` | property `-> list[str]` | All `Sitemap:` URLs in the file |
| `allow_all` | classmethod `-> RobotsPolicy` | Convenience — allows everything |

**Allow-all cases**: empty text, whitespace-only, no valid User-agent groups, or `Disallow:` with an empty value.

## Parsing rules

- **Longest-match wins**: `/foo/bar` beats `/foo` for path `/foo/bar/page`.
- **`*` wildcard**: matches any sequence of characters (including empty).
- **`$` anchor**: anchors the pattern to the end of the path (`/page$` matches `/page` but not `/page/sub`).
- **Multiple `User-agent:` lines** before any directive share the same rule group.
- **Blank line** separates groups; a new `User-agent:` after directives also starts a new group.
- **Comments** (`#` to end of line) are stripped.
- **`Crawl-delay:`** and **`Sitemap:`** are parsed but do not affect `is_allowed`.

## `RobotsCache`

`RobotsCache(ttl=3600, fetcher=None, clock=None)` — async fetch + TTL cache.

| Param | Default | Notes |
|-------|---------|-------|
| `ttl` | `3600.0` | Seconds before re-fetching |
| `fetcher` | HTTP via aiohttp | `async (url: str) -> str` — injectable for tests |
| `clock` | `time.monotonic` | `() -> float` — injectable for deterministic tests |

```python
# Test with injected fetcher + clock
t = [0.0]
cache = RobotsCache(ttl=60, fetcher=my_fake_fetcher, clock=lambda: t[0])
policy = await cache.get("https://example.com")
t[0] = 61.0  # expire the cache
policy = await cache.get("https://example.com")  # re-fetches
```

## Opt-in only

`RobotsCache` is **never instantiated** in the default scrape or poll path. Adding a `RobotsCache` call to your code is the only way robots.txt is ever fetched. A no-config deploy behaves identically to before this feature was added.

## Future: learned rate limiting

`crawl_delay()` values are intended as future inputs to the `ujin.adapt.concurrency` learned-rate-limit system, so per-host crawl delays declared in robots.txt can automatically shape the token-bucket rate.
