"""The ujin plugin registry — resolve source/transform/sink/scorer *kinds*.

Built-in kinds (``http``/``api``/``select``/``webhook`` …) and plugin-contributed
kinds live in one registry, looked up identically. A job spec references a
built-in by bare name (``"webhook"``) or a plugin by ``"plugin:my_sink"``; both
resolve here.

Plugin authors get a tiny decorator surface::

    from ujin import register

    @register.source("my_api")
    def make(cfg):                      # (config) -> Pollable
        return MyApiPollable(cfg["url"])

    @register.sink("my_db")
    def make_sink(cfg):                 # (config) -> Sink
        return MyDbSink(cfg["dsn"])

Context-needing built-ins (the ``scrape`` source, the ``ws``/``sqlite`` sinks)
receive a :class:`BuildContext` — plugin factories that take a second parameter
get it too; single-parameter factories (the common case) do not.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("ujin.registry")


@dataclass
class BuildContext:
    """Ambient services a factory may need. All optional."""

    scrape_service: Any = None
    hub: Any = None
    store: Any = None
    browser: Any = None  # live BrowserFetcher (for action factories)
    page: Any = None     # live page handle for the current browser run


@dataclass
class _Entry:
    factory: Callable[..., Any]
    builtin: bool


class Registry:
    """One namespace per category (source/transform/sink/scorer)."""

    _CATEGORIES = ("source", "transform", "sink", "scorer", "action")

    def __init__(self) -> None:
        self._maps: dict[str, dict[str, _Entry]] = {c: {} for c in self._CATEGORIES}

    # -- decorators (public plugin surface) -------------------------------- #
    def source(self, name: str) -> Callable:
        return self._decorator("source", name, builtin=False)

    def transform(self, name: str) -> Callable:
        return self._decorator("transform", name, builtin=False)

    def sink(self, name: str) -> Callable:
        return self._decorator("sink", name, builtin=False)

    def scorer(self, name: str) -> Callable:
        return self._decorator("scorer", name, builtin=False)

    def action(self, name: str) -> Callable:
        """Register a custom browser interaction step (a recipe action).

        The factory ``(cfg, ctx) -> async def handler(page, **params)`` receives a
        :class:`BuildContext` carrying the live ``browser``/``page``.
        """
        return self._decorator("action", name, builtin=False)

    def _decorator(self, category: str, name: str, *, builtin: bool) -> Callable:
        def deco(fn: Callable) -> Callable:
            self._maps[category][name] = _Entry(factory=fn, builtin=builtin)
            return fn

        return deco

    def register_builtin(self, category: str, name: str, factory: Callable) -> None:
        self._maps[category][name] = _Entry(factory=factory, builtin=True)

    # -- resolution -------------------------------------------------------- #
    @staticmethod
    def _normalize(kind: str) -> str:
        return kind[len("plugin:"):] if kind.startswith("plugin:") else kind

    def has(self, category: str, kind: str) -> bool:
        return self._normalize(kind) in self._maps[category]

    def available(self, category: str) -> list[str]:
        return sorted(self._maps[category])

    def _build(self, category: str, kind: str, cfg: dict, ctx: BuildContext | None):
        name = self._normalize(kind)
        entry = self._maps[category].get(name)
        if entry is None:
            raise KeyError(
                f"unknown {category} kind {kind!r}; "
                f"available: {', '.join(self.available(category)) or '(none)'}"
            )
        cfg = cfg or {}
        # pass the context only to factories that declare a second parameter
        try:
            nparams = len(inspect.signature(entry.factory).parameters)
        except (TypeError, ValueError):
            nparams = 1
        if nparams >= 2:
            return entry.factory(cfg, ctx or BuildContext())
        return entry.factory(cfg)

    def build_source(self, kind: str, cfg: dict, ctx: BuildContext | None = None):
        return self._build("source", kind, cfg, ctx)

    def build_transform(self, kind: str, cfg: dict, ctx: BuildContext | None = None):
        return self._build("transform", kind, cfg, ctx)

    def build_sink(self, kind: str, cfg: dict, ctx: BuildContext | None = None):
        return self._build("sink", kind, cfg, ctx)

    def build_scorer(self, kind: str, cfg: dict, ctx: BuildContext | None = None):
        return self._build("scorer", kind, cfg, ctx)

    def build_action(self, kind: str, cfg: dict, ctx: BuildContext | None = None):
        return self._build("action", kind, cfg, ctx)

    # -- hot reload -------------------------------------------------------- #
    def clear_plugins(self) -> None:
        """Drop every plugin-contributed entry, keep built-ins."""
        for category, mapping in self._maps.items():
            for name in [n for n, e in mapping.items() if not e.builtin]:
                del mapping[name]


# The global registry. Built-ins are installed below; plugins add to it via the
# decorators when their module is imported by ujin.plugins.loader.
register = Registry()


def _install_builtins(reg: Registry) -> None:
    # --- sources (lazy poll imports keep the core dependency-light) ------- #
    def _src_http(cfg):
        from ujin.poll.http import HttpPollable

        return HttpPollable(cfg["url"], render=cfg.get("render", False))

    def _src_rss(cfg):
        from ujin.poll.rss import RssPollable

        return RssPollable(cfg["url"])

    def _src_api(cfg):
        from ujin.poll.api import ApiPollable

        return ApiPollable(
            cfg["url"], method=cfg.get("method", "GET"),
            json_path=cfg.get("json_path"), headers=cfg.get("headers"),
            json_body=cfg.get("json_body"),
        )

    def _src_command(cfg):
        from ujin.poll.command import CommandPollable

        return CommandPollable(cfg["argv"])

    def _src_site(cfg):
        from ujin.poll.site import SitePollable

        return SitePollable(cfg["url"], cfg.get("selectors"),
                            render=cfg.get("render", False))

    def _src_scrape(cfg, ctx: BuildContext):
        if ctx.scrape_service is None:
            raise ValueError("scrape source needs the scrape backend (jobs[scrape])")
        from ujin.poll.scrape import ScrapePollable

        return ScrapePollable(
            ctx.scrape_service, cfg["url"], mode=cfg.get("mode", "links"),
            force_refresh=cfg.get("force_refresh", False),
        )

    def _src_browser(cfg, ctx: BuildContext):
        from ujin.poll.browser import BrowserPollable

        return BrowserPollable(
            cfg["url"], engine=cfg.get("engine", "playwright"),
            actions=cfg.get("actions", []), extract=cfg.get("extract", "links"),
            results_selector=cfg.get("results_selector"),
            headless=cfg.get("headless", True), ctx=ctx,
        )

    def _src_graphql(cfg):
        from ujin.poll.graphql import GraphQLPollable

        return GraphQLPollable(
            cfg["url"],
            query=cfg["query"],
            variables=cfg.get("variables"),
            headers=cfg.get("headers"),
            data_path=cfg.get("data_path"),
        )

    reg.register_builtin("source", "http", _src_http)
    reg.register_builtin("source", "rss", _src_rss)
    reg.register_builtin("source", "api", _src_api)
    reg.register_builtin("source", "graphql", _src_graphql)
    reg.register_builtin("source", "command", _src_command)
    reg.register_builtin("source", "site", _src_site)
    reg.register_builtin("source", "scrape", _src_scrape)
    reg.register_builtin("source", "browser", _src_browser)

    # --- transforms ------------------------------------------------------- #
    from ujin.jobs.transforms import BUILTIN_TRANSFORMS

    # NB: a closure, not `lambda c, _cls=tcls: ...` — a default second param
    # makes _build think the factory wants the BuildContext and clobbers it.
    def _mk_transform(tcls):
        def factory(cfg):
            return tcls(cfg)
        return factory

    for tname, tcls in BUILTIN_TRANSFORMS.items():
        reg.register_builtin("transform", tname, _mk_transform(tcls))

    # --- sinks (ctx carries hub + store) ---------------------------------- #
    from ujin.jobs.sinks import BUILTIN_SINKS, build_sink

    for sname in BUILTIN_SINKS:
        reg.register_builtin(
            "sink", sname,
            (lambda c, ctx, _n=sname: build_sink(_n, c, hub=ctx.hub, store=ctx.store)),
        )


_install_builtins(register)
