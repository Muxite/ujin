"""Static capability matrix for the four fetch backends.

One queryable source of truth for what each backend can and cannot do, plus a
live availability probe. Consumed by ``GET /capabilities`` on the scrape
service, the MCP ``get_capabilities`` tool, and documented in
``docs/BACKENDS.md`` (keep the three in sync).

The numbers are order-of-magnitude characterizations measured against the
benchmark suite (see ``benchmarks/``), not guarantees: real-world speed is
dominated by the target site.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Literal

JsRendering = Literal["none", "full"]


@dataclass(frozen=True)
class BackendCapability:
    name: str
    description: str
    js_rendering: JsRendering
    anti_bot_evasion: Literal["low", "medium", "high"]
    relative_speed: Literal["fastest", "fast", "slow", "slowest"]
    typical_latency_ms: tuple[int, int]      # (warm, cold) order of magnitude
    memory_per_page_mb: int                  # rough RSS cost per concurrent page
    max_concurrency: int                     # sane per-process ceiling
    install_weight: str                      # what it takes to enable
    interaction: bool                        # can run click/fill/load_more recipes
    conditional_get: bool                    # ETag / If-Modified-Since support
    availability_check: Callable[[], bool]

    def snapshot(self) -> dict:
        """JSON-safe dict including the *current* availability."""
        d = asdict(self)
        d.pop("availability_check")
        d["available"] = self.available()
        return d

    def available(self) -> bool:
        try:
            return bool(self.availability_check())
        except Exception:  # noqa: BLE001 - a probe must never take the caller down
            return False


def _http_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("aiohttp") is not None


def _obscura_check() -> bool:
    from .obscura import obscura_available

    return obscura_available()


def _playwright_check() -> bool:
    from .browser import browser_available

    return browser_available("playwright")


def _selenium_check() -> bool:
    from .browser import browser_available

    return browser_available("selenium")


BACKENDS: dict[str, BackendCapability] = {
    "http": BackendCapability(
        name="http",
        description="aiohttp GET with shared session, per-host semaphore, "
                    "conditional GET. The default first leg of every scrape.",
        js_rendering="none",
        anti_bot_evasion="low",          # python TLS fingerprint, no JS challenge
        relative_speed="fastest",
        typical_latency_ms=(50, 500),
        memory_per_page_mb=1,
        max_concurrency=64,              # TCPConnector pool limit
        install_weight="pure pip (ujin[web])",
        interaction=False,
        conditional_get=True,
        availability_check=_http_available,
    ),
    "obscura": BackendCapability(
        name="obscura",
        description="Bundled Rust headless renderer (binary or HTTP service). "
                    "Static JS snapshot — executes scripts, no interaction.",
        js_rendering="full",
        anti_bot_evasion="medium",       # real JS engine, but headless signals remain
        relative_speed="fast",
        typical_latency_ms=(800, 5000),
        memory_per_page_mb=150,
        max_concurrency=4,
        install_weight="cargo build via `ujin obscura-build` (~15-20 min first "
                       "build) or OBSCURA_URL service",
        interaction=False,
        conditional_get=False,
        availability_check=_obscura_check,
    ),
    "playwright": BackendCapability(
        name="playwright",
        description="Full Chromium automation (async). Runs interaction "
                    "recipes: click, fill, load_more, scroll, eval_js, screenshots.",
        js_rendering="full",
        anti_bot_evasion="medium",       # headless Chromium is fingerprintable
        relative_speed="slow",
        typical_latency_ms=(1500, 10000),
        memory_per_page_mb=300,
        max_concurrency=4,
        install_weight="pip ujin[browser] + `playwright install chromium` (~280MB)",
        interaction=True,
        conditional_get=False,
        availability_check=_playwright_check,
    ),
    "selenium": BackendCapability(
        name="selenium",
        description="Chromedriver automation (blocking WebDriver marshalled to "
                    "one thread). Same recipes as playwright; the fallback engine.",
        js_rendering="full",
        anti_bot_evasion="low",          # webdriver flag widely detected
        relative_speed="slowest",
        typical_latency_ms=(2000, 15000),
        memory_per_page_mb=350,
        max_concurrency=1,               # single marshalling thread per fetcher
        install_weight="pip ujin[browser] + system chromedriver + chrome/chromium",
        interaction=True,
        conditional_get=False,
        availability_check=_selenium_check,
    ),
}


def capabilities_snapshot() -> dict[str, dict]:
    """All backends with live availability — the /capabilities payload."""
    return {name: cap.snapshot() for name, cap in BACKENDS.items()}
