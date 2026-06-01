"""Runtime configuration for the scrape service.

This replaces jennie/scraper-v2's ``pydantic-settings`` ``Settings`` with a
plain stdlib dataclass so the scrape stack carries no hard dependency on
``pydantic-settings`` and stays injectable for tests. Field defaults and the
``from_env`` alias names match scraper-v2 exactly, so an existing deployment's
environment keeps working unchanged.

Usage::

    cfg = ScrapeConfig()              # all defaults
    cfg = ScrapeConfig.from_env()     # read the same env vars scraper-v2 used
    cfg = ScrapeConfig(host_cooldown_secs=120)  # override individual fields
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ScrapeConfig:
    log_level: str = "INFO"

    # Fetch / render.
    obscura_bin: str = "obscura"
    fetch_timeout_secs: int = 30
    http_timeout_secs: int = 15
    user_agent: str = "Mozilla/5.0 (compatible; ujin-scrape/0.3)"

    # Social (Brave search) key.
    brave_api_key: str = ""

    # Cache.
    cache_max_entries: int = 2048
    cache_ttl_secs: int = 120
    disk_cache_path: str = ""

    # Per-host policy.
    per_host_concurrency: int = 2
    host_cooldown_secs: int = 60
    fast_path_min_links: int = 5
    per_host_config_path: str = ""

    # Batch.
    batch_max_items: int = 64

    # Social chain.
    nitter_pool_path: str = ""
    x_allow_brave: bool = True

    # When true, the scrape app wires a corroboration store, an x-trends
    # refresh loop, and a BreakingScorer (news-trading semantics). Off by
    # default — the generic app uses NullScorer.
    enable_breaking_scorer: bool = False

    # Trends / corroboration (used only when a BreakingScorer is wired).
    corroboration_window_secs: int = 1800
    corroboration_min_hosts: int = 3
    corroboration_max_hosts_for_full_score: int = 5
    headline_ring_max: int = 8192

    # Breaking-tier thresholds + weights (sum of weights == 1.0).
    breaking_threshold: float = 0.40
    developing_threshold: float = 0.25
    tier_weight_source_rank: float = 0.20
    tier_weight_lede_marker: float = 0.10
    tier_weight_recency: float = 0.15
    tier_weight_corroboration: float = 0.45
    tier_weight_trend_overlap: float = 0.10

    # Maps each field to the env-var name scraper-v2 used (its pydantic alias).
    _ENV_ALIASES = {
        "log_level": "LOG_LEVEL",
        "obscura_bin": "OBSCURA_BIN",
        "fetch_timeout_secs": "FETCH_TIMEOUT_SECS",
        "http_timeout_secs": "HTTP_TIMEOUT_SECS",
        "user_agent": "SCRAPER_USER_AGENT",
        "brave_api_key": "SEARCH_API_KEY",
        "cache_max_entries": "CACHE_MAX_ENTRIES",
        "cache_ttl_secs": "CACHE_TTL_SECS",
        "disk_cache_path": "DISK_CACHE_PATH",
        "per_host_concurrency": "PER_HOST_CONCURRENCY",
        "host_cooldown_secs": "HOST_COOLDOWN_SECS",
        "fast_path_min_links": "FAST_PATH_MIN_LINKS",
        "per_host_config_path": "PER_HOST_CONFIG_PATH",
        "batch_max_items": "BATCH_MAX_ITEMS",
        "nitter_pool_path": "NITTER_POOL_PATH",
        "x_allow_brave": "X_ALLOW_BRAVE",
        "enable_breaking_scorer": "UJIN_BREAKING_SCORER",
        "corroboration_window_secs": "CORROBORATION_WINDOW_SECS",
        "corroboration_min_hosts": "CORROBORATION_MIN_HOSTS",
        "corroboration_max_hosts_for_full_score": "CORROBORATION_MAX_HOSTS",
        "headline_ring_max": "HEADLINE_RING_MAX",
        "breaking_threshold": "BREAKING_THRESHOLD",
        "developing_threshold": "DEVELOPING_THRESHOLD",
        "tier_weight_source_rank": "TIER_W_SOURCE_RANK",
        "tier_weight_lede_marker": "TIER_W_LEDE_MARKER",
        "tier_weight_recency": "TIER_W_RECENCY",
        "tier_weight_corroboration": "TIER_W_CORROBORATION",
        "tier_weight_trend_overlap": "TIER_W_TREND_OVERLAP",
    }

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ScrapeConfig":
        """Build a config from environment variables (defaults for any unset).

        ``env`` defaults to ``os.environ``; pass a dict in tests. Values are
        coerced to each field's declared type (``int``/``float``/``bool``).
        """
        src = os.environ if env is None else env
        kwargs: dict[str, object] = {}
        types = {f.name: f.type for f in fields(cls)}
        for name, alias in cls._ENV_ALIASES.items():
            raw = src.get(alias)
            if raw is None:
                continue
            kwargs[name] = _coerce(raw, types[name])
        return cls(**kwargs)  # type: ignore[arg-type]


def _coerce(raw: str, type_hint: object) -> object:
    """Coerce an env string to int/float/bool/str based on the field type."""
    # Field types are strings under ``from __future__ import annotations``.
    hint = type_hint if isinstance(type_hint, str) else getattr(type_hint, "__name__", "str")
    if hint == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if hint == "int":
        return int(raw)
    if hint == "float":
        return float(raw)
    return raw
