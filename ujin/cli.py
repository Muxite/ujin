"""ujin command line.

  ujin serve targets.yaml      run the poll engine as a daemon
  ujin sweep targets.yaml      one pass; print which targets changed

targets.yaml::

    rate: 10            # global requests/sec (smoothing)
    burst: 10
    concurrency: 8
    defaults: { base: 60, min: 5, max: 3600, jitter: decorrelated }
    targets:
      - http:    { url: https://example.com }
      - rss:     { url: https://example.com/feed.xml, base: 300 }
      - api:     { url: https://api.example.com/v1/x, json_path: data.items }
      - command: { argv: [git, ls-remote, https://github.com/x/y] }
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("ujin.cli")


def _build_pollable(kind: str, cfg: dict[str, Any]):
    if kind == "http":
        from ujin.poll.http import HttpPollable

        return HttpPollable(cfg["url"], render=cfg.get("render", False))
    if kind == "rss":
        from ujin.poll.rss import RssPollable

        return RssPollable(cfg["url"])
    if kind == "api":
        from ujin.poll.api import ApiPollable

        return ApiPollable(cfg["url"], method=cfg.get("method", "GET"),
                           json_path=cfg.get("json_path"), headers=cfg.get("headers"))
    if kind == "command":
        from ujin.poll.command import CommandPollable

        return CommandPollable(cfg["argv"])
    if kind == "site":
        from ujin.poll.site import SitePollable

        return SitePollable(cfg["url"], cfg.get("selectors"),
                            render=cfg.get("render", False))
    raise ValueError(f"unknown target kind: {kind!r}")


def _load(path: str):
    import yaml

    from ujin.adapt.concurrency import TokenBucket
    from ujin.engine import PollEngine

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults", {})
    engine = PollEngine(
        token_bucket=TokenBucket(rate=data.get("rate", 10.0), burst=data.get("burst", 10.0)),
        max_concurrency=data.get("concurrency", 8),
    )

    async def _on_change(key: str, result) -> None:
        log.info("CHANGED %s (fp=%s)", key, (result.fingerprint or "")[:12])

    for entry in data.get("targets", []):
        kind, cfg = next(iter(entry.items()))
        pollable = _build_pollable(kind, cfg or {})
        engine.add(
            pollable,
            base=cfg.get("base", defaults.get("base", 60)),
            min_interval=cfg.get("min", defaults.get("min", 5)),
            max_interval=cfg.get("max", defaults.get("max", 3600)),
            jitter=cfg.get("jitter", defaults.get("jitter", "decorrelated")),
            on_change=_on_change,
        )
    return engine


def _cmd_serve(args: argparse.Namespace) -> int:
    engine = _load(args.targets)
    log.info("ujin serve: %d target(s)", len(engine.targets))
    asyncio.run(engine.run())
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    engine = _load(args.targets)
    results = asyncio.run(engine.sweep())
    changed = [t.key for t in engine.targets.values() if t.prev and t.prev.changed]
    print(f"swept {len(results)} target(s); changed: {changed or 'none'}")
    return 0


def _cmd_api(args: argparse.Namespace) -> int:
    from ujin.service import serve

    serve(host=args.host, port=args.port, config_path=args.targets)
    return 0


def _cmd_scrape_serve(args: argparse.Namespace) -> int:
    from ujin.scrape.app import serve
    from ujin.scrape.config import ScrapeConfig

    serve(host=args.host, port=args.port, config=ScrapeConfig.from_env())
    return 0


def _cmd_obscura_build(args: argparse.Namespace) -> int:
    """Init the bundled obscura submodule and build the release binary.

    This is the only step that needs the Rust toolchain; it is never run at
    pip-install time. The first build compiles V8 and is slow (~15-20 min).
    """
    import subprocess

    repo_root = Path(__file__).resolve().parents[1].parent
    submodule = repo_root / "ujin" / "obscura"
    log.info("initializing obscura submodule at %s", submodule)
    subprocess.run(
        ["git", "submodule", "update", "--init", "ujin/obscura"],
        cwd=repo_root, check=True,
    )
    if not (submodule / "Cargo.toml").exists():
        log.error("obscura submodule has no Cargo.toml at %s", submodule)
        return 1
    log.info("building obscura (cargo build --release) — first build is slow")
    subprocess.run(["cargo", "build", "--release"], cwd=submodule, check=True)
    binary = submodule / "target" / "release" / "obscura"
    log.info("obscura built: %s", binary)
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Watch one URL's selected regions; log or webhook on change."""
    from ujin.diff.events import CallbackSink, WebhookSink
    from ujin.engine import PollEngine
    from ujin.poll.site import SitePollable

    pollable = SitePollable(args.url, args.selector or None, render=args.render)
    if args.webhook:
        on_change = WebhookSink(args.webhook)
    else:
        def _log(event) -> None:
            log.info("CHANGED %s regions=%s fp=%s",
                     event.key, event.regions or "(whole-page)",
                     (event.fingerprint or "")[:12])

        on_change = CallbackSink(_log)

    engine = PollEngine()
    engine.add(pollable, base=args.base, min_interval=args.min,
               max_interval=args.max, on_change=on_change)
    log.info("ujin watch: %s (%d selector(s))", args.url, len(args.selector or []))
    asyncio.run(engine.run())
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="ujin", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the poll engine (daemon)")
    p_serve.add_argument("targets")
    p_serve.set_defaults(func=_cmd_serve)

    p_sweep = sub.add_parser("sweep", help="poll all targets once")
    p_sweep.add_argument("targets")
    p_sweep.set_defaults(func=_cmd_sweep)

    p_api = sub.add_parser("api", help="serve the REST + WebSocket API")
    p_api.add_argument("targets", nargs="?", default=None,
                       help="optional targets.yaml to preload")
    p_api.add_argument("--host", default="0.0.0.0")
    p_api.add_argument("--port", type=int, default=8900)
    p_api.set_defaults(func=_cmd_api)

    p_scrape = sub.add_parser(
        "scrape-serve", help="serve the rich scrape HTTP API (/scrape /feed ...)"
    )
    p_scrape.add_argument("--host", default="0.0.0.0")
    p_scrape.add_argument("--port", type=int, default=8901)
    p_scrape.set_defaults(func=_cmd_scrape_serve)

    p_watch = sub.add_parser(
        "watch", help="watch a URL's regions for change (adaptive, jittered)"
    )
    p_watch.add_argument("url")
    p_watch.add_argument("--selector", action="append", default=[],
                         help="CSS selector to watch (repeatable; omit for whole page)")
    p_watch.add_argument("--webhook", default=None,
                         help="POST change events here (default: log)")
    p_watch.add_argument("--render", action="store_true",
                         help="render via obscura before extracting")
    p_watch.add_argument("--base", type=float, default=60.0)
    p_watch.add_argument("--min", type=float, default=5.0)
    p_watch.add_argument("--max", type=float, default=3600.0)
    p_watch.set_defaults(func=_cmd_watch)

    p_obs = sub.add_parser(
        "obscura-build", help="init + build the bundled obscura renderer (needs cargo)"
    )
    p_obs.set_defaults(func=_cmd_obscura_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
