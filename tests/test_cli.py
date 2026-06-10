"""CLI arg dispatch and the YAML target loader — serve functions mocked."""
from __future__ import annotations

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
