"""ujin command line — adaptive scraper-poller.

Run ``ujin <command> --help`` for any command's options and examples.

  ujin doctor                  show which backends/extras are installed
  ujin init                    scaffold a starter targets.yaml in the cwd
  ujin sweep targets.yaml      poll all targets once; print what changed
  ujin serve targets.yaml      run the poll engine as a daemon
  ujin watch URL --selector …  watch one page's regions for change

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


def _version() -> str:
    """Best-effort package version (the in-package attr, then installed metadata).

    The hardcoded ``ujin.__version__`` is the release source of truth and tracks
    the working tree; installed ``.dist-info`` metadata can lag behind it under
    an editable install, so prefer the attr and fall back to metadata.
    """
    try:
        import ujin

        v = getattr(ujin, "__version__", None)
        if v:
            return v
    except Exception:  # noqa: BLE001 - never let version lookup break the CLI
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("ujin")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _build_pollable(kind: str, cfg: dict[str, Any]):
    """Resolve a poll source through the plugin registry.

    Built-in kinds (http/rss/api/command/site/scrape/browser) and any
    plugin-registered ``plugin:*`` source kinds resolve identically here, so the
    YAML-driven engine and the jobs control plane share one code path.
    """
    from ujin.registry import register

    if not register.has("source", kind):
        raise ValueError(
            f"unknown source kind {kind!r}; "
            f"available: {', '.join(register.available('source'))}"
        )
    try:
        return register.build_source(kind, cfg or {})
    except KeyError as exc:
        # The kind resolved; the factory hit a missing config key (e.g. `url`).
        raise ValueError(f"missing required config key {exc}") from None


def _read_yaml(path: str) -> dict[str, Any]:
    """Load a YAML file into a dict, with actionable file+line errors."""
    import yaml

    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"ujin: targets file not found: {path}\n"
            f"  hint: run `ujin init` to scaffold a starter targets.yaml"
        )
    text = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        loc = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            loc = f" (line {mark.line + 1}, column {mark.column + 1})"
        raise SystemExit(f"ujin: invalid YAML in {path}{loc}: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SystemExit(
            f"ujin: {path} must be a YAML mapping with a `targets:` list, "
            f"got {type(data).__name__}"
        )
    return data


def _load(path: str):
    from ujin.adapt.concurrency import TokenBucket
    from ujin.engine import PollEngine

    data = _read_yaml(path)
    defaults = data.get("defaults", {})
    engine = PollEngine(
        token_bucket=TokenBucket(rate=data.get("rate", 10.0), burst=data.get("burst", 10.0)),
        max_concurrency=data.get("concurrency", 8),
    )

    async def _on_change(key: str, result) -> None:
        log.info("CHANGED %s (fp=%s)", key, (result.fingerprint or "")[:12])

    for i, entry in enumerate(data.get("targets", [])):
        if not isinstance(entry, dict) or not entry:
            raise SystemExit(
                f"ujin: {path} targets[{i}] must be a single-key mapping like "
                f"`- http: {{url: ...}}`, got {entry!r}"
            )
        kind, cfg = next(iter(entry.items()))
        try:
            pollable = _build_pollable(kind, cfg or {})
        except ValueError as exc:
            raise SystemExit(f"ujin: {path} targets[{i}] ({kind}): {exc}") from None
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


def _cmd_jobs_serve(args: argparse.Namespace) -> int:
    from ujin.jobs.app import serve

    serve(host=args.host, port=args.port, config_path=args.jobs,
          workflows_dir=args.workflows)
    return 0


def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    from ujin.mcp import serve

    serve(transport=("http" if args.http else "stdio"),
          host=args.host, port=args.port)
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


_STARTER_TARGETS = """\
# ujin targets — poll anything on an adaptive, jittered cadence.
#   ujin sweep targets.yaml    one pass; prints what changed
#   ujin serve targets.yaml    run as a daemon
#
# Global smoothing knobs (optional):
rate: 10            # aggregate requests/sec ceiling (token bucket)
burst: 10
concurrency: 8

# Per-target interval defaults (seconds). Each target may override these.
defaults: { base: 60, min: 5, max: 3600, jitter: decorrelated }

targets:
  # An HTTP page — fingerprinted whole-body change detection.
  - http: { url: https://example.com }
  # An RSS/Atom feed, polled less often.
  - rss: { url: https://example.com/feed.xml, base: 300 }
  # A JSON API; json_path narrows to the slice that matters.
  - api: { url: https://api.example.com/v1/items, json_path: data.items }
  # Any shell command — change = different stdout.
  - command: { argv: [git, ls-remote, https://github.com/python/cpython] }
"""


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Report installed backends/extras and what each unlocks."""
    from ujin.fetch.capabilities import BACKENDS

    print(f"ujin {_version()}")
    print()
    print("Fetch backends (what renders/fetches your pages):")
    for cap in BACKENDS.values():
        ok = cap.available()
        mark = "ok " if ok else "-- "
        print(f"  [{mark}] {cap.name:<10} {'available' if ok else 'not installed'}")
        print(f"            {cap.description.splitlines()[0]}")
        if not ok:
            print(f"            enable: {cap.install_weight}")

    # Optional Python extras, probed without importing heavy modules.
    import importlib.util

    extras = [
        ("aiohttp", "web", "HTTP/RSS/API roles + the scrape toolkit"),
        ("selectolax", "scrape", "fast HTML parsing for extraction"),
        ("trafilatura", "scrape", "article/main-content extraction"),
        ("feedparser", "web", "RSS/Atom feed parsing"),
        ("fastapi", "service", "the REST/WebSocket HTTP services (api/scrape/jobs-serve)"),
        ("mcp", "mcp", "the MCP server for agents (ujin mcp-serve)"),
    ]
    print()
    print("Python extras:")
    for mod, extra, what in extras:
        present = importlib.util.find_spec(mod) is not None
        mark = "ok " if present else "-- "
        hint = "" if present else f"   (pip install 'ujin[{extra}]')"
        print(f"  [{mark}] {mod:<13} {what}{hint}")
    print()
    print("Tip: `ujin init` writes a starter targets.yaml; "
          "`ujin sweep targets.yaml` runs one pass.")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a starter targets.yaml so a newcomer can run `ujin sweep` at once."""
    dest = Path(args.path)
    if dest.exists() and not args.force:
        print(f"ujin: {dest} already exists (use --force to overwrite)",
              file=sys.stderr)
        return 1
    dest.write_text(_STARTER_TARGETS, encoding="utf-8")
    print(f"wrote {dest}")
    print(f"next: ujin sweep {dest}    # one pass, prints what changed")
    print(f"      ujin serve {dest}    # run as an adaptive daemon")
    return 0


def _open_site_store_ro(path: str | None):
    """Open an *existing* ``SiteStore`` for read-only introspection.

    The store API has no record-creating side effects beyond ``record()`` (which
    we never call here), but ``sqlite3.connect`` happily *creates* a missing
    file — so we check existence first and emit a clean, actionable ``ujin: ...``
    error (no traceback) for a missing or empty path, matching the rest of the
    CLI's error style.
    """
    import sqlite3

    from ujin.adapt import SiteStore

    if not path:
        raise SystemExit(
            "ujin: a site-store database path is required\n"
            "  usage: ujin learned <site_state.db>  "
            "(the path your poller/scraper persists to)"
        )
    if not Path(path).exists():
        raise SystemExit(
            f"ujin: site-store database not found: {path}\n"
            "  hint: pass the SiteStore db path your poller/scraper persists to"
        )
    try:
        return SiteStore(path)
    except sqlite3.DatabaseError:
        raise SystemExit(f"ujin: not a valid ujin site-store database: {path}") from None


def _open_strategy_feedback_ro(path: str):
    """Open an existing ``StrategyFeedback`` db read-only (same error style)."""
    import sqlite3

    from ujin.adapt import StrategyFeedback

    if not Path(path).exists():
        raise SystemExit(
            f"ujin: strategy-feedback database not found: {path}\n"
            "  hint: pass the StrategyFeedback db path your scraper persists to"
        )
    try:
        return StrategyFeedback(path)
    except sqlite3.DatabaseError:
        raise SystemExit(
            f"ujin: not a valid ujin strategy database: {path}"
        ) from None


def _fmt_secs(value: float) -> str:
    """Compact seconds with trailing zeros stripped, e.g. ``10s``/``0.42s``."""
    s = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return f"{s}s"


_LEARNED_COLUMNS = [
    ("host", "host", str),
    ("status", "last_status", str),
    ("latency", "last_latency", _fmt_secs),
    ("p50", "p50_latency", _fmt_secs),
    ("interval", "interval", _fmt_secs),
    ("rec.int", "recommended_interval", _fmt_secs),
    ("conc", "concurrency_factor", lambda v: f"{v:.2f}"),
    ("health", "health", lambda v: f"{v:.2f}"),
    ("cooldown", "cooldown_secs", lambda v: _fmt_secs(v) if v else "-"),
    ("crawl", "crawl_delay", lambda v: _fmt_secs(v) if v else "-"),
    ("err", "error_count", str),
    ("429", "rate_limit_count", str),
]


def _print_learned_table(args: argparse.Namespace, rows: list[dict]) -> None:
    """Render the per-host learned state as an aligned, human-readable table."""
    if not rows:
        if args.host:
            print(f"(host {args.host!r} not found in {args.db})")
        else:
            print(f"(no learned hosts in {args.db})")
        return
    cols = list(_LEARNED_COLUMNS)
    if args.strategy_db:
        cols.append(
            ("best-strategy", "recommended_strategy",
             lambda v: "/".join(v) if v else "-")
        )
    headers = [label for label, _, _ in cols]
    cells = [[fmt(r[key]) for _, key, fmt in cols] for r in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in cells))
        for i in range(len(headers))
    ]
    fmt_row = lambda values: "  ".join(  # noqa: E731 - tiny local formatter
        v.ljust(w) for v, w in zip(values, widths)
    )
    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print(fmt_row(row))


def _cmd_learned(args: argparse.Namespace) -> int:
    """Inspect the durable per-host learned state in a SiteStore database."""
    import json as _json

    from ujin.adapt import derive_signals

    store = _open_site_store_ro(args.db)
    feedback = _open_strategy_feedback_ro(args.strategy_db) if args.strategy_db else None
    try:
        known = store.hosts()
        if args.host:
            hosts = [args.host] if args.host in known else []
        else:
            hosts = known

        rows: list[dict] = []
        for host in hosts:
            rec = store.get(host)
            # The stored adaptive interval is the base; derive_signals layers any
            # rate-limit slowdown and Crawl-delay floor on top of it.
            sig = derive_signals(rec, base_interval=rec.interval)
            row = {
                "host": host,
                "last_status": rec.last_status,
                "last_latency": round(rec.last_latency, 6),
                "p50_latency": round(rec.p50_latency, 6),
                "error_count": rec.error_count,
                "rate_limit_count": rec.rate_limit_count,
                "crawl_delay": rec.crawl_delay,
                "interval": rec.interval,
                "last_seen": rec.last_seen,
                "recommended_interval": round(sig.recommended_interval, 6),
                "concurrency_factor": round(sig.concurrency_factor, 6),
                "health": round(sig.health, 6),
                "rate_limited": sig.rate_limited,
                "should_cooldown": sig.should_cooldown,
                "cooldown_secs": round(sig.cooldown_secs, 6),
            }
            if feedback is not None:
                best = feedback.recommend(host)
                row["recommended_strategy"] = list(best) if best else None
            rows.append(row)

        if args.json:
            print(_json.dumps({"db": args.db, "hosts": rows}, indent=2))
        else:
            _print_learned_table(args, rows)
        return 0
    finally:
        store.close()
        if feedback is not None:
            feedback.close()


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
    parser = argparse.ArgumentParser(
        prog="ujin",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="New here? Try `ujin doctor` then `ujin init`. "
               "Docs: https://github.com/Muxite/ujin (see README + docs/).",
    )
    parser.add_argument("--version", action="version",
                        version=f"ujin {_version()}")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    p_doctor = sub.add_parser(
        "doctor", help="report installed backends/extras and what each unlocks",
        description="Show which fetch backends (http/obscura/playwright/selenium) "
                    "and optional Python extras are installed, and the pip command "
                    "to enable each missing one. Safe to run any time.",
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_init = sub.add_parser(
        "init", help="scaffold a starter targets.yaml in the current directory",
        description="Write a commented starter targets.yaml you can run "
                    "immediately with `ujin sweep`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin init && ujin sweep targets.yaml",
    )
    p_init.add_argument("path", nargs="?", default="targets.yaml",
                        help="output file (default: targets.yaml)")
    p_init.add_argument("-f", "--force", action="store_true",
                        help="overwrite an existing file")
    p_init.set_defaults(func=_cmd_init)

    p_serve = sub.add_parser(
        "serve", help="run the poll engine (daemon)",
        description="Load targets.yaml and run the adaptive poll engine until "
                    "interrupted. Use `sweep` for a one-shot cron-style pass.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin serve targets.yaml",
    )
    p_serve.add_argument("targets", help="path to a targets.yaml (see `ujin init`)")
    p_serve.set_defaults(func=_cmd_serve)

    p_sweep = sub.add_parser(
        "sweep", help="poll all targets once and print what changed",
        description="Poll every target once and print which ones changed since "
                    "last run. Cron-friendly; exits when done.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin sweep targets.yaml",
    )
    p_sweep.add_argument("targets", help="path to a targets.yaml (see `ujin init`)")
    p_sweep.set_defaults(func=_cmd_sweep)

    p_api = sub.add_parser(
        "api", help="serve the poller control REST + WebSocket API (:8900)",
        description="Run the poller control plane: GET /health /metrics /targets, "
                    "POST /targets /sweep, WS /ws. Set UJIN_API_KEY to require auth.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin api targets.yaml --port 8900",
    )
    p_api.add_argument("targets", nargs="?", default=None,
                       help="optional targets.yaml to preload")
    p_api.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    p_api.add_argument("--port", type=int, default=8900, help="port (default: 8900)")
    p_api.set_defaults(func=_cmd_api)

    p_scrape = sub.add_parser(
        "scrape-serve", help="serve the rich scrape HTTP API (/scrape /feed ...) (:8901)",
        description="Run the one-shot scrape service: POST /scrape (modes "
                    "links|article|auto|combined|structured), /feed, /sitemap, "
                    "/discover, /capabilities. Configured via env (see docs/API.md).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin scrape-serve --port 8901   "
               "# then: curl localhost:8901/capabilities",
    )
    p_scrape.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    p_scrape.add_argument("--port", type=int, default=8901, help="port (default: 8901)")
    p_scrape.set_defaults(func=_cmd_scrape_serve)

    p_jobs = sub.add_parser(
        "jobs-serve", help="serve the unified job control plane (/jobs ...) (:8902)",
        description="Run the durable job control plane (source->transforms->sinks->"
                    "schedule). Preload jobs from a YAML arg and/or workflow files "
                    "from --workflows. State persists to $UJIN_JOBS_DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin jobs-serve                              # empty, durable\n"
               "  ujin jobs-serve examples/jobs.crossref.yaml  # preload jobs\n"
               "  ujin jobs-serve --workflows ./workflows      # file-driven",
    )
    p_jobs.add_argument("jobs", nargs="?", default=None,
                        help="optional jobs.yaml to preload")
    p_jobs.add_argument("--workflows", default=None,
                        help="directory of workflow files to load (default: "
                             "$UJIN_WORKFLOWS_DIR or /workflows)")
    p_jobs.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    p_jobs.add_argument("--port", type=int, default=8902, help="port (default: 8902)")
    p_jobs.set_defaults(func=_cmd_jobs_serve)

    p_watch = sub.add_parser(
        "watch", help="watch a URL's regions for change (adaptive, jittered)",
        description="Poll one URL on the adaptive engine and report when the "
                    "selected regions change. Omit --selector to watch the whole "
                    "page; pass --webhook to POST change events instead of logging.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin watch https://example.com\n"
               "  ujin watch https://example.com --selector main --selector .price\n"
               "  ujin watch https://example.com --webhook https://hooks/me",
    )
    p_watch.add_argument("url", help="page URL to watch")
    p_watch.add_argument("--selector", action="append", default=[], metavar="CSS",
                         help="CSS selector to watch (repeatable; omit for whole page)")
    p_watch.add_argument("--webhook", default=None, metavar="URL",
                         help="POST change events here (default: log to stdout)")
    p_watch.add_argument("--render", action="store_true",
                         help="render via obscura before extracting (JS pages)")
    p_watch.add_argument("--base", type=float, default=60.0,
                         help="starting interval in seconds (default: 60)")
    p_watch.add_argument("--min", type=float, default=5.0,
                         help="fastest interval in seconds (default: 5)")
    p_watch.add_argument("--max", type=float, default=3600.0,
                         help="slowest interval in seconds (default: 3600)")
    p_watch.set_defaults(func=_cmd_watch)

    p_learned = sub.add_parser(
        "learned", help="inspect the durable per-host learned state in a SiteStore db",
        description="Open an existing SiteStore database read-only and print, per "
                    "host, the learned adaptive state: recommended interval (via "
                    "ujin.adapt.derive_signals), concurrency factor, penalty/backoff "
                    "(health, cooldown, rate-limited), last observed status/latency, "
                    "and any observed robots Crawl-delay. With --strategy-db, also "
                    "show the recommended (backend, render_mode) for each host. "
                    "Defaults to a table; --json emits machine-readable output; "
                    "--host filters to one host.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin learned site_state.db\n"
               "  ujin learned site_state.db --host example.com --json\n"
               "  ujin learned site_state.db --strategy-db strategy.db",
    )
    p_learned.add_argument("db", nargs="?", default=None,
                           help="path to a SiteStore database (the path your "
                                "poller/scraper persists to)")
    p_learned.add_argument("--host", default=None, metavar="HOST",
                           help="show only this host (default: every learned host)")
    p_learned.add_argument("--strategy-db", default=None, metavar="PATH",
                           help="also show StrategyFeedback.recommend(host) from "
                                "this strategy-feedback database")
    p_learned.add_argument("--json", action="store_true",
                           help="emit machine-readable JSON instead of a table")
    p_learned.set_defaults(func=_cmd_learned)

    p_mcp = sub.add_parser(
        "mcp-serve", help="run the MCP server for agents (stdio; --http for HTTP)",
        description="Expose scraping + the job control plane as MCP tools for "
                    "Claude Code / Desktop or any MCP client. Defaults to stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin mcp-serve                       # stdio (for `claude mcp add`)\n"
               "  ujin mcp-serve --http --port 8903    # streamable HTTP",
    )
    p_mcp.add_argument("--http", action="store_true",
                       help="streamable HTTP transport instead of stdio")
    p_mcp.add_argument("--host", default="127.0.0.1",
                       help="bind address for --http (default: 127.0.0.1)")
    p_mcp.add_argument("--port", type=int, default=8903,
                       help="port for --http (default: 8903)")
    p_mcp.set_defaults(func=_cmd_mcp_serve)

    p_obs = sub.add_parser(
        "obscura-build", help="init + build the bundled obscura renderer (needs cargo)",
        description="Init the obscura git submodule and build its release binary "
                    "(needs the Rust toolchain). The first build compiles V8 and is "
                    "slow (~15-20 min). Never run at pip-install time.",
    )
    p_obs.set_defaults(func=_cmd_obscura_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
