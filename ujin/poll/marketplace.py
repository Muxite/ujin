"""Generic, profile-driven marketplace search — the *engine*, not the site specifics.

ujin ships the site-agnostic machinery (sample a profile's keyterms per poll, scrape each
via :class:`~ujin.poll.amazon.AmazonSearchPollable`, dedupe, combine). It does NOT ship any
site profiles. A **profile** is plain data describing how to search and read one site, and is
supplied by the caller — either inline in the job/source config or from a file/volume mount
via ``UJIN_MARKETPLACE_PROFILES`` (so the specific scraping config can live in and be owned
by the consuming program, e.g. wordle-max).

Profile schema (all keys but ``domain``/``search_url`` optional)::

    <name>:
      domain: ebay.com
      search_url: "https://www.{domain}/sch/?_nkw={query}"   # {domain},{query} filled in
      selectors:            # CSS overrides for the product card; null => JSON-LD/OG defaults
        card: ".s-card, .s-item"
        id_attr: "data-id"
        title: [".s-card__title", "h3"]
        image: [".s-card__image img", "img"]
        price: [".s-card__price"]
        link: ["a.su-link", "a[href*='/itm/']"]
      engine: browser       # auto | http | browser
      wait_selector: ".s-card"
      desc_selectors: [...]  # optional, for with_description detail scraping
      keyterms:             # category -> sample terms (the per-poll sampling bank)
        Electronics: ["wireless earbuds", "graphics card"]

See ``examples/marketplace_profiles.yaml`` for a ready-to-mount reference set
(amazon / newegg / ebay / walmart) and ``docs/MARKETPLACE.md`` for the full guide.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random as _random_mod
from pathlib import Path

from ujin.poll.amazon import AmazonSearchPollable
from ujin.poll.base import PollResult, decide_changed, fingerprint

log = logging.getLogger("ujin.poll.marketplace")

#: Env var holding a path to a YAML/JSON profile file (mountable as a volume).
PROFILES_ENV = "UJIN_MARKETPLACE_PROFILES"


def _load_file(path: str | Path) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"marketplace profiles file not found: {p}")
    text = p.read_text()
    if p.suffix == ".json":
        data = json.loads(text)
    else:  # .yaml/.yml or unknown -> YAML (a superset of JSON)
        import yaml  # from the `yaml` extra

        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"profiles file {p} must be a mapping of name -> profile")
    return data


def load_profiles(
    inline: dict[str, dict] | None = None,
    path: str | Path | None = None,
) -> dict[str, dict]:
    """Resolve site profiles from a file (``path`` arg or ``$UJIN_MARKETPLACE_PROFILES``)
    and/or an ``inline`` mapping. Inline entries override file entries of the same name.
    ujin ships no built-in profiles, so the result is empty unless a source is given."""
    profiles: dict[str, dict] = {}
    src = path or os.environ.get(PROFILES_ENV)
    if src:
        profiles.update(_load_file(src))
    if inline:
        profiles.update(inline)
    return profiles


class MarketplaceSearchPollable:
    """Scrape a sample of a site profile's keyterms per poll -> combined product list.

    The profile is resolved at construction from (in precedence order) ``profiles`` inline,
    then the ``profiles_path`` file, then ``$UJIN_MARKETPLACE_PROFILES``. An unknown profile
    name is a hard error — ujin no longer ships site specifics.
    """

    def __init__(
        self,
        *,
        profile: str = "amazon",
        profiles: dict[str, dict] | None = None,
        profiles_path: str | None = None,
        categories: dict[str, list[str]] | None = None,
        terms_per_poll: int = 3,
        max_results: int = 8,
        engine: str | None = None,
        proxy: str | None = None,
        timeout_secs: int = 40,
        headless: bool = True,
        seed: int | None = None,
        with_description: bool = False,
        key: str | None = None,
    ) -> None:
        available = load_profiles(inline=profiles, path=profiles_path)
        if profile not in available:
            raise ValueError(
                f"unknown marketplace profile {profile!r}; available: "
                f"{sorted(available) or '(none)'}. Provide profiles inline, via "
                f"profiles_path, or the {PROFILES_ENV} env var (see docs/MARKETPLACE.md)."
            )
        self.profile_name = profile
        self.profile = available[profile]
        self.categories = categories or self.profile.get("keyterms") or {}
        self.terms_per_poll = max(1, int(terms_per_poll))
        self.max_results = max(1, int(max_results))
        self.engine = engine or self.profile.get("engine", "auto")
        self.proxy = proxy
        self.timeout_secs = timeout_secs
        self.headless = headless
        self.with_description = with_description
        self.key = key or f"marketplace:{self.profile_name}"
        self._rng = _random_mod.Random(seed)

    def _child(self, term: str, category: str | None) -> AmazonSearchPollable:
        return AmazonSearchPollable(
            term,
            domain=self.profile["domain"],
            max_results=self.max_results,
            category=category,
            engine=self.engine,
            headless=self.headless,
            proxy=self.proxy,
            timeout_secs=self.timeout_secs,
            source=self.profile_name,
            selectors=self.profile.get("selectors"),
            search_url_template=self.profile["search_url"],
            wait_selector=self.profile.get("wait_selector"),
            with_description=self.with_description,
            desc_selectors=self.profile.get("desc_selectors"),
        )

    def _sample(self) -> list[tuple[str, str]]:
        pairs = [(t, cat) for cat, terms in self.categories.items() for t in terms]
        if not pairs:
            return []
        k = min(self.terms_per_poll, len(pairs))
        return self._rng.sample(pairs, k)

    async def poll(self, prev: PollResult | None) -> PollResult:
        pairs = self._sample()
        log.info("marketplace[%s] sweep: %s", self.profile_name, [t for t, _ in pairs])
        children = [self._child(term, cat) for term, cat in pairs]
        results = await asyncio.gather(*(c.poll(None) for c in children), return_exceptions=True)
        combined: list[dict] = []
        seen: set[str] = set()
        for res in results:
            if isinstance(res, Exception) or not getattr(res, "ok", False):
                continue
            for item in (res.payload or []):
                sid = item.get("source_id")
                if sid and sid in seen:
                    continue
                if sid:
                    seen.add(sid)
                combined.append(item)
        return PollResult(
            ok=True,
            changed=decide_changed(fingerprint(combined), prev),
            fingerprint=fingerprint(combined),
            payload=combined,
        )
