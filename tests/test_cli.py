"""CLI arg dispatch and the YAML target loader — serve functions mocked."""
from __future__ import annotations

import argparse
import sys

import pytest

import ujin.cli as cli


TARGETS_YAML = """\
rate: 5.0
burst: 5.0
concurrency: 4
defaults:
  base: 120
  jitter: none
targets:
  - command:
      argv: ["echo", "hi"]
      base: 30
  - command:
      argv: ["echo", "bye"]
"""


def test_load_builds_engine_from_yaml(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text(TARGETS_YAML)
    engine = cli._load(str(p))
    assert len(engine.targets) == 2
    bases = sorted(t.interval.base for t in engine.targets.values())
    assert bases == [30, 120]  # per-target override beats defaults


def test_load_empty_yaml(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("")
    assert len(cli._load(str(p)).targets) == 0


def test_build_pollable_unknown_kind():
    with pytest.raises(ValueError, match="unknown source kind"):
        cli._build_pollable("warp", {})


def test_build_pollable_builtin():
    p = cli._build_pollable("command", {"argv": ["true"]})
    assert hasattr(p, "poll")


def test_main_no_command_exits_with_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_main_sweep_runs(tmp_path, capsys):
    p = tmp_path / "targets.yaml"
    p.write_text(TARGETS_YAML)
    rc = cli.main(["sweep", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "swept 2 target(s)" in out


def test_main_api_dispatch(monkeypatch):
    called = {}

    def fake_serve(host, port, config_path):
        called.update(host=host, port=port, config_path=config_path)

    monkeypatch.setattr("ujin.service.serve", fake_serve)
    rc = cli.main(["api", "--host", "127.0.0.1", "--port", "9999"])
    assert rc == 0
    assert called["host"] == "127.0.0.1" and called["port"] == 9999


def test_main_scrape_serve_dispatch(monkeypatch):
    called = {}

    def fake_serve(host, port, config):
        called.update(host=host, port=port)

    monkeypatch.setattr("ujin.scrape.app.serve", fake_serve)
    rc = cli.main(["scrape-serve", "--port", "18901"])
    assert rc == 0
    assert called["port"] == 18901


def test_main_jobs_serve_dispatch(monkeypatch):
    called = {}

    def fake_serve(host, port, config_path, workflows_dir):
        called.update(port=port, workflows_dir=workflows_dir)

    monkeypatch.setattr("ujin.jobs.app.serve", fake_serve)
    rc = cli.main(["jobs-serve", "--workflows", "/wf"])
    assert rc == 0
    assert called["workflows_dir"] == "/wf"


def test_main_serve_dispatch(monkeypatch, tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text(TARGETS_YAML)
    ran = {}

    async def fake_run(self):
        ran["targets"] = len(self.targets)

    monkeypatch.setattr("ujin.engine.PollEngine.run", fake_run)
    rc = cli.main(["serve", str(p)])
    assert rc == 0
    assert ran["targets"] == 2


# ── --version ───────────────────────────────────────────────────────────────

def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "ujin" in capsys.readouterr().out.lower()


def test_version_helper_returns_string():
    assert isinstance(cli._version(), str) and cli._version()


def test_version_helper_falls_back_to_package_attr(monkeypatch):
    import importlib.metadata as md

    def boom(_name):
        raise md.PackageNotFoundError("ujin")

    monkeypatch.setattr(md, "version", boom)
    # falls through to ujin.__version__ without raising
    assert isinstance(cli._version(), str)


def test_mcp_serve_stdio_dispatch(monkeypatch):
    pytest.importorskip("mcp")
    called = {}

    def fake_serve(transport, host, port):
        called.update(transport=transport, port=port)

    # `ujin.mcp.serve` resolves lazily to `ujin.mcp.server.serve` via __getattr__,
    # so patch the canonical attribute (matches test_mcp_server.py).
    monkeypatch.setattr("ujin.mcp.server.serve", fake_serve)
    rc = cli.main(["mcp-serve"])  # stdio default (no --http)
    assert rc == 0
    assert called["transport"] == "stdio"


# ── doctor ──────────────────────────────────────────────────────────────────

def test_doctor_reports_backends_and_extras(capsys):
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    # every fetch backend is listed
    for name in ("http", "obscura", "playwright", "selenium"):
        assert name in out
    # extras section names the pip extra to enable a missing one
    assert "Python extras" in out
    assert "fastapi" in out


# ── init ────────────────────────────────────────────────────────────────────

def test_init_writes_loadable_starter(tmp_path, capsys):
    dest = tmp_path / "targets.yaml"
    rc = cli.main(["init", str(dest)])
    assert rc == 0
    assert dest.exists()
    assert "wrote" in capsys.readouterr().out
    # the scaffold must parse and build an engine without touching the network
    engine = cli._load(str(dest))
    assert len(engine.targets) == 4  # http, rss, api, command


def test_init_refuses_to_clobber_without_force(tmp_path, capsys):
    dest = tmp_path / "targets.yaml"
    dest.write_text("existing")
    rc = cli.main(["init", str(dest)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    assert dest.read_text() == "existing"


def test_init_force_overwrites(tmp_path):
    dest = tmp_path / "targets.yaml"
    dest.write_text("existing")
    rc = cli.main(["init", str(dest), "--force"])
    assert rc == 0
    assert "ujin targets" in dest.read_text()


def test_init_default_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["init"])
    assert rc == 0
    assert (tmp_path / "targets.yaml").exists()


# ── actionable load errors (no tracebacks; SystemExit with a hint) ───────────

def test_load_missing_file_is_actionable():
    with pytest.raises(SystemExit) as exc:
        cli._load("/no/such/targets.yaml")
    msg = str(exc.value)
    assert "not found" in msg and "ujin init" in msg


def test_load_invalid_yaml_names_line(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("targets:\n  - http: {url: x\n  bad: : indent\n")
    with pytest.raises(SystemExit) as exc:
        cli._load(str(p))
    msg = str(exc.value)
    assert "invalid YAML" in msg and "line" in msg


def test_load_non_mapping_document(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("- just a list\n")
    with pytest.raises(SystemExit) as exc:
        cli._load(str(p))
    assert "must be a YAML mapping" in str(exc.value)


def test_load_non_mapping_target_entry(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("targets:\n  - just a string\n")
    with pytest.raises(SystemExit) as exc:
        cli._load(str(p))
    assert "single-key mapping" in str(exc.value)


def test_load_unknown_kind_lists_valid_kinds(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("targets:\n  - warp: {url: x}\n")
    with pytest.raises(SystemExit) as exc:
        cli._load(str(p))
    msg = str(exc.value)
    assert "unknown source kind 'warp'" in msg
    assert "http" in msg and "rss" in msg  # valid kinds listed


def test_load_missing_required_config_key(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("targets:\n  - http: {render: true}\n")  # no url
    with pytest.raises(SystemExit) as exc:
        cli._load(str(p))
    assert "missing required config key" in str(exc.value)
    assert "url" in str(exc.value)


def test_build_pollable_unknown_kind_message_lists_kinds():
    with pytest.raises(ValueError) as exc:
        cli._build_pollable("nope", {})
    assert "available:" in str(exc.value)


def test_load_empty_document_is_empty_engine(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("# just a comment\n")
    assert len(cli._load(str(p)).targets) == 0


# ── _version() fallback paths (lines 48-59) ──────────────────────────────────

def test_version_import_ujin_exception_uses_metadata(monkeypatch):
    """Lines 48-49: import ujin raises; metadata fallback returns a version."""
    monkeypatch.setitem(sys.modules, "ujin", None)
    v = cli._version()
    assert isinstance(v, str) and v


def test_version_package_not_found_returns_unknown(monkeypatch):
    """Lines 48-49, 55-56, 59: import fails + PackageNotFoundError → 'unknown'."""
    import importlib.metadata as _md

    monkeypatch.setitem(sys.modules, "ujin", None)

    def _boom(name):
        raise _md.PackageNotFoundError(name)

    monkeypatch.setattr(_md, "version", _boom)
    assert cli._version() == "unknown"


def test_version_metadata_import_blocked_returns_unknown(monkeypatch):
    """Lines 57-58, 59: importlib.metadata blocked entirely → 'unknown'."""
    monkeypatch.setitem(sys.modules, "ujin", None)
    monkeypatch.setitem(sys.modules, "importlib.metadata", None)
    assert cli._version() == "unknown"


# ── YAML error without problem_mark (branch 99->101) ─────────────────────────

def test_load_yaml_error_without_mark(monkeypatch, tmp_path):
    """Branch 99->101: YAMLError with no problem_mark emits 'invalid YAML' without line info."""
    import yaml

    p = tmp_path / "t.yaml"
    p.write_text("x: 1")
    bare_exc = yaml.YAMLError("no mark here")

    def _raiser(_text):
        raise bare_exc

    monkeypatch.setattr(yaml, "safe_load", _raiser)
    with pytest.raises(SystemExit) as exc:
        cli._load(str(p))
    assert "invalid YAML" in str(exc.value)


# ── obscura-build command (lines 200-216) ────────────────────────────────────

def test_obscura_build_success(monkeypatch, tmp_path):
    """Lines 200-216 happy path: git submodule + cargo called, returns 0."""
    fake_file = tmp_path / "root" / "d" / "ujin" / "cli.py"
    fake_file.parent.mkdir(parents=True)
    submodule = tmp_path / "root" / "ujin" / "obscura"
    submodule.mkdir(parents=True)
    (submodule / "Cargo.toml").write_text("[workspace]\n")

    monkeypatch.setattr(cli, "__file__", str(fake_file))
    runs = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: runs.append(cmd))

    rc = cli._cmd_obscura_build(argparse.Namespace())
    assert rc == 0
    assert any("git" in str(c) for c in runs)
    assert any("cargo" in str(c) for c in runs)


def test_obscura_build_missing_cargo_toml_returns_1(monkeypatch, tmp_path):
    """Lines 209-211: git runs but Cargo.toml absent → returns 1."""
    fake_file = tmp_path / "root" / "d" / "ujin" / "cli.py"
    fake_file.parent.mkdir(parents=True)

    monkeypatch.setattr(cli, "__file__", str(fake_file))
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: None)

    rc = cli._cmd_obscura_build(argparse.Namespace())
    assert rc == 1


# ── watch command (lines 299-319) ────────────────────────────────────────────

def test_watch_callback_sink(monkeypatch):
    """Lines 299-319 no-webhook path: CallbackSink used, engine runs."""
    async def _fake_run(self):
        pass

    monkeypatch.setattr("ujin.engine.PollEngine.run", _fake_run)
    rc = cli.main(["watch", "http://example.com"])
    assert rc == 0


def test_watch_webhook_sink(monkeypatch):
    """Lines 304-305: --webhook path creates WebhookSink."""
    async def _fake_run(self):
        pass

    monkeypatch.setattr("ujin.engine.PollEngine.run", _fake_run)
    rc = cli.main(["watch", "http://example.com", "--webhook", "http://hook.test"])
    assert rc == 0


def test_watch_with_selectors_and_render(monkeypatch):
    """Lines 315-317: multiple --selector args and --render flag."""
    async def _fake_run(self):
        pass

    monkeypatch.setattr("ujin.engine.PollEngine.run", _fake_run)
    rc = cli.main([
        "watch", "http://example.com",
        "--selector", "h1", "--selector", ".price",
        "--render",
    ])
    assert rc == 0


# ── learned (read-only SiteStore introspection) ──────────────────────────────

def _seed_site_store(path, **rows):
    """Write some hosts into a SiteStore db at ``path`` and close it.

    ``rows`` maps host -> dict of record() signals.
    """
    from ujin.adapt import SiteStore

    s = SiteStore(str(path))
    for host, signals in rows.items():
        s.record(host, **signals)
    s.close()


def test_main_help_lists_learned(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "learned" in capsys.readouterr().out


def test_learned_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["learned", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "learned" in out and "--strategy-db" in out


def test_learned_json_includes_recorded_host_and_fields(tmp_path, capsys):
    """Acceptance: record a host, close, then `ujin learned <db> --json` shows it."""
    import json

    db = tmp_path / "site.db"
    _seed_site_store(db, **{
        "example.com": dict(status=200, latency=0.4, interval=10.0, crawl_delay=2.0),
    })

    rc = cli.main(["learned", str(db), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "example.com" in out
    data = json.loads(out)
    hosts = {h["host"]: h for h in data["hosts"]}
    assert "example.com" in hosts
    h = hosts["example.com"]
    assert h["last_status"] == 200
    assert h["last_latency"] == 0.4
    assert h["crawl_delay"] == 2.0
    assert h["interval"] == 10.0
    # derived fields are present
    for field in ("recommended_interval", "concurrency_factor", "health",
                  "rate_limited", "should_cooldown", "cooldown_secs"):
        assert field in h


def test_learned_table_is_default(tmp_path, capsys):
    db = tmp_path / "site.db"
    _seed_site_store(db, **{"example.com": dict(status=200, latency=0.4)})
    rc = cli.main(["learned", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "example.com" in out
    assert "host" in out and "rec.int" in out  # header row present


def test_learned_host_filters_to_one(tmp_path, capsys):
    import json

    db = tmp_path / "site.db"
    _seed_site_store(db, **{
        "a.com": dict(status=200),
        "b.com": dict(status=500),
    })
    rc = cli.main(["learned", str(db), "--host", "a.com", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert [h["host"] for h in data["hosts"]] == ["a.com"]
    assert "b.com" not in out


def test_learned_with_strategy_db_shows_recommendation(tmp_path, capsys):
    import json

    from ujin.adapt import StrategyFeedback

    db = tmp_path / "site.db"
    _seed_site_store(db, **{"a.com": dict(status=200)})
    sdb = tmp_path / "strategy.db"
    fb = StrategyFeedback(str(sdb))
    fb.record("a.com", ("obscura", "js"), ok=True, latency=0.3)
    fb.close()

    rc = cli.main(["learned", str(db), "--strategy-db", str(sdb), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    h = json.loads(out)["hosts"][0]
    assert h["recommended_strategy"] == ["obscura", "js"]
    # the table form surfaces it as a column too
    rc = cli.main(["learned", str(db), "--strategy-db", str(sdb)])
    assert rc == 0
    assert "obscura/js" in capsys.readouterr().out


def test_learned_empty_store_is_clean(tmp_path, capsys):
    from ujin.adapt import SiteStore

    db = tmp_path / "site.db"
    SiteStore(str(db)).close()  # exists but no hosts
    rc = cli.main(["learned", str(db)])
    assert rc == 0
    assert "no learned hosts" in capsys.readouterr().out


def test_learned_host_not_found_is_clean(tmp_path, capsys):
    db = tmp_path / "site.db"
    _seed_site_store(db, **{"a.com": dict(status=200)})
    rc = cli.main(["learned", str(db), "--host", "ghost.com"])
    assert rc == 0
    assert "not found" in capsys.readouterr().out


def test_learned_no_db_arg_is_actionable():
    with pytest.raises(SystemExit) as exc:
        cli.main(["learned"])
    msg = str(exc.value)
    assert msg.startswith("ujin:") and "required" in msg


def test_learned_missing_db_is_actionable(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["learned", str(tmp_path / "nope.db")])
    msg = str(exc.value)
    assert msg.startswith("ujin:") and "not found" in msg


def test_learned_missing_strategy_db_is_actionable(tmp_path):
    db = tmp_path / "site.db"
    _seed_site_store(db, **{"a.com": dict(status=200)})
    with pytest.raises(SystemExit) as exc:
        cli.main(["learned", str(db), "--strategy-db", str(tmp_path / "nope.db")])
    msg = str(exc.value)
    assert msg.startswith("ujin:") and "not found" in msg


def test_learned_corrupt_db_is_actionable(tmp_path):
    db = tmp_path / "site.db"
    db.write_text("this is not a sqlite database")
    with pytest.raises(SystemExit) as exc:
        cli.main(["learned", str(db)])
    assert "not a valid ujin site-store" in str(exc.value)
