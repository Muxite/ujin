"""Workflow `defaults:` deep-merge + `include:`/`use:` fragment resolution.

Offline and deterministic: every test writes YAML to a tmp_path and parses it
through the loader helpers in :mod:`ujin.jobs.app`. No network, no live server.

Covered: deep-merge precedence, file-level vs per-job includes, the `use:` alias,
nested includes, cycle detection, missing-fragment errors, path resolution via
$UJIN_WORKFLOWS_DIR, the directory loader's log-and-skip behaviour, the
`--config` preload path, the shipped example, and a regression proving legacy
single-/multi-job files plus ${VAR} substitution parse exactly as before.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ujin.jobs.app import (
    WorkflowIncludeError,
    _deep_merge,
    _load_workflows_dir,
    _preload_specs,
    _specs_from_workflow_file,
)

_ROOT = Path(__file__).resolve().parents[1]


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


# ── _deep_merge unit ─────────────────────────────────────────────────────────


def test_deep_merge_recurses_and_does_not_mutate():
    base = {"schedule": {"mode": "adaptive", "base": 300, "min": 60}, "enabled": True}
    override = {"schedule": {"base": 600}, "enabled": False}
    out = _deep_merge(base, override)
    # nested map merged key-by-key; per-key override wins, siblings preserved
    assert out == {
        "schedule": {"mode": "adaptive", "base": 600, "min": 60},
        "enabled": False,
    }
    # inputs untouched
    assert base["schedule"]["base"] == 300 and base["enabled"] is True


def test_deep_merge_replaces_lists_wholesale():
    base = {"sinks": [{"kind": "sqlite"}]}
    override = {"sinks": [{"kind": "jsonl"}]}
    assert _deep_merge(base, override) == {"sinks": [{"kind": "jsonl"}]}


# ── defaults: deep-merge precedence ──────────────────────────────────────────


def test_defaults_deep_merge_per_job_overrides(tmp_path):
    f = _write(
        tmp_path / "feeds.yaml",
        """
defaults:
  schedule:
    mode: adaptive
    base: 300
    min: 60
  enabled: true
jobs:
  - source: { kind: command, config: { argv: [echo, a] } }
  - source: { kind: command, config: { argv: [echo, b] } }
    schedule:
      base: 600
    enabled: false
""",
    )
    specs = _specs_from_workflow_file(f)
    assert len(specs) == 2

    # job 0 inherits all defaults
    assert specs[0].schedule.base == 300
    assert specs[0].schedule.min == 60
    assert specs[0].enabled is True

    # job 1 overrides schedule.base (recursive) + enabled, inherits schedule.min
    assert specs[1].schedule.base == 600
    assert specs[1].schedule.min == 60
    assert specs[1].enabled is False


def test_defaults_merge_into_single_job_mapping(tmp_path):
    f = _write(
        tmp_path / "solo.yaml",
        """
defaults:
  source:
    kind: api
    config:
      method: GET
source:
  config:
    url: "https://example.com/api"
""",
    )
    (spec,) = _specs_from_workflow_file(f)
    # default source.kind + config.method merge under the inline config.url
    assert spec.id == "solo" and spec.source.kind == "api"
    assert spec.source.config == {"method": "GET", "url": "https://example.com/api"}


# ── include:/use: fragments ──────────────────────────────────────────────────


def test_file_level_include_applies_to_every_job(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(
        frag / "sink.yaml",
        "transforms: [{ kind: dedupe, config: { key: link } }]\n"
        "sinks: [{ kind: sqlite }]\n",
    )
    f = _write(
        tmp_path / "feeds.yaml",
        """
include: frags/sink.yaml
jobs:
  - source: { kind: command, config: { argv: [echo, a] } }
  - source: { kind: command, config: { argv: [echo, b] } }
""",
    )
    specs = _specs_from_workflow_file(f)
    for s in specs:
        assert [t.kind for t in s.transforms] == ["dedupe"]
        assert [k.kind for k in s.sinks] == ["sqlite"]


def test_per_job_include_with_use_alias(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "sqlite.yaml", "sinks: [{ kind: sqlite }]\n")
    f = _write(
        tmp_path / "feeds.yaml",
        """
jobs:
  - source: { kind: command, config: { argv: [echo, a] } }
    use: frags/sqlite.yaml
  - source: { kind: command, config: { argv: [echo, b] } }
""",
    )
    specs = _specs_from_workflow_file(f)
    assert [k.kind for k in specs[0].sinks] == ["sqlite"]
    assert specs[1].sinks == []  # second job did not include the fragment


def test_include_accepts_a_list_of_fragments(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "a.yaml", "transforms: [{ kind: dedupe, config: { key: id } }]\n")
    _write(frag / "b.yaml", "sinks: [{ kind: sqlite }]\n")
    f = _write(
        tmp_path / "solo.yaml",
        """
include:
  - frags/a.yaml
  - frags/b.yaml
source: { kind: command, config: { argv: [echo, a] } }
""",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert [t.kind for t in spec.transforms] == ["dedupe"]
    assert [k.kind for k in spec.sinks] == ["sqlite"]


def test_include_absolute_path(tmp_path, monkeypatch):
    monkeypatch.delenv("UJIN_WORKFLOWS_DIR", raising=False)
    shared = _write(tmp_path / "shared.yaml", "sinks: [{ kind: sqlite }]\n")
    f = _write(
        tmp_path / "solo.yaml",
        f"include: {shared}\n"  # absolute path used as-is
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert [k.kind for k in spec.sinks] == ["sqlite"]


def test_top_level_list_file_with_per_job_include(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "sink.yaml", "sinks: [{ kind: sqlite }]\n")
    # A bare top-level list (no `jobs:` key) — carries no file-level directives.
    f = _write(
        tmp_path / "listed.yaml",
        """
- name: a
  source: { kind: command, config: { argv: [echo, a] } }
  use: frags/sink.yaml
- name: b
  source: { kind: command, config: { argv: [echo, b] } }
""",
    )
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["listed-0", "listed-1"]
    assert [k.kind for k in specs[0].sinks] == ["sqlite"]
    assert specs[1].sinks == []


def test_inline_keys_win_over_fragment(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "sched.yaml", "schedule: { mode: adaptive, base: 999 }\n")
    f = _write(
        tmp_path / "solo.yaml",
        """
include: frags/sched.yaml
source: { kind: command, config: { argv: [echo, a] } }
schedule:
  base: 42
""",
    )
    (spec,) = _specs_from_workflow_file(f)
    # precedence: fragment < inline → base=42, mode inherited from fragment
    assert spec.schedule.base == 42
    assert spec.schedule.mode == "adaptive"


def test_precedence_defaults_lt_fragment_lt_inline(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "f.yaml", "schedule: { base: 200, min: 30 }\n")
    f = _write(
        tmp_path / "solo.yaml",
        """
defaults:
  schedule: { base: 100, min: 10, max: 7200 }
include: frags/f.yaml
source: { kind: command, config: { argv: [echo, a] } }
schedule:
  base: 300
""",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert spec.schedule.base == 300   # inline wins
    assert spec.schedule.min == 30     # fragment beats defaults
    assert spec.schedule.max == 7200   # only defaults set it


# ── nested includes + cycle guard ────────────────────────────────────────────


def test_nested_includes_resolve(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "outer.yaml", "include: inner.yaml\nsinks: [{ kind: sqlite }]\n")
    _write(frag / "inner.yaml", "transforms: [{ kind: dedupe, config: { key: id } }]\n")
    f = _write(
        tmp_path / "solo.yaml",
        "include: frags/outer.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert [t.kind for t in spec.transforms] == ["dedupe"]
    assert [k.kind for k in spec.sinks] == ["sqlite"]


def test_cyclic_include_raises(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "a.yaml", "include: b.yaml\nsinks: [{ kind: sqlite }]\n")
    _write(frag / "b.yaml", "include: a.yaml\ntransforms: []\n")
    f = _write(
        tmp_path / "solo.yaml",
        "include: frags/a.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    with pytest.raises(WorkflowIncludeError, match="cyclic include"):
        _specs_from_workflow_file(f)


def test_cross_directory_cyclic_include_raises(tmp_path):
    # A cycle that crosses directories via ``..``: frags/loop.yaml pulls in
    # ../top.yaml, which pulls frags/loop.yaml back. Without path normalisation
    # the ``..`` candidate never string-matches the chain key and recursion runs
    # away into an OS "File name too long" error instead of a clear cyclic error.
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(tmp_path / "top.yaml", "include: frags/loop.yaml\ntransforms: []\n")
    _write(frag / "loop.yaml", "include: ../top.yaml\nsinks: [{ kind: sqlite }]\n")
    f = _write(
        tmp_path / "solo.yaml",
        "include: frags/loop.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    with pytest.raises(WorkflowIncludeError, match="cyclic include"):
        _specs_from_workflow_file(f)


def test_missing_fragment_raises(tmp_path):
    f = _write(
        tmp_path / "solo.yaml",
        "include: frags/nope.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    with pytest.raises(WorkflowIncludeError, match="not found"):
        _specs_from_workflow_file(f)


def test_bad_include_type_raises(tmp_path):
    f = _write(
        tmp_path / "solo.yaml",
        "include: 123\nsource: { kind: command, config: { argv: [echo, a] } }\n",
    )
    with pytest.raises(WorkflowIncludeError, match="fragment path"):
        _specs_from_workflow_file(f)


def test_non_mapping_fragment_raises(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "list.yaml", "- a\n- b\n")
    f = _write(
        tmp_path / "solo.yaml",
        "include: frags/list.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    with pytest.raises(WorkflowIncludeError, match="must be a mapping"):
        _specs_from_workflow_file(f)


# ── path resolution via $UJIN_WORKFLOWS_DIR ──────────────────────────────────


def test_include_resolves_via_workflows_dir_first(tmp_path, monkeypatch):
    # Fragment lives in the workflows dir, NOT next to the including file.
    wf_dir = tmp_path / "wf"
    wf_dir.mkdir()
    _write(wf_dir / "shared.yaml", "sinks: [{ kind: sqlite }]\n")
    monkeypatch.setenv("UJIN_WORKFLOWS_DIR", str(wf_dir))

    other = tmp_path / "elsewhere"
    other.mkdir()
    f = _write(
        other / "solo.yaml",
        "include: shared.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert [k.kind for k in spec.sinks] == ["sqlite"]


def test_workflows_dir_takes_precedence_over_sibling(tmp_path, monkeypatch):
    # Same basename in both locations: the workflows dir wins.
    wf_dir = tmp_path / "wf"
    wf_dir.mkdir()
    _write(wf_dir / "shared.yaml", "sinks: [{ kind: sqlite }]\n")
    monkeypatch.setenv("UJIN_WORKFLOWS_DIR", str(wf_dir))

    here = tmp_path / "here"
    here.mkdir()
    _write(here / "shared.yaml", "sinks: [{ kind: stdout }]\n")  # sibling decoy
    f = _write(
        here / "solo.yaml",
        "include: shared.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert [k.kind for k in spec.sinks] == ["sqlite"]  # not stdout


def test_include_falls_back_to_sibling_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("UJIN_WORKFLOWS_DIR", raising=False)
    _write(tmp_path / "shared.yaml", "sinks: [{ kind: sqlite }]\n")
    f = _write(
        tmp_path / "solo.yaml",
        "include: shared.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert [k.kind for k in spec.sinks] == ["sqlite"]


# ── directory loader: log-and-skip, surface in `failed` ──────────────────────


def test_dir_loader_skips_bad_include(tmp_path, caplog):
    _write(
        tmp_path / "good.yaml",
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    _write(
        tmp_path / "bad.yaml",
        "include: missing.yaml\n"
        "source: { kind: command, config: { argv: [echo, b] } }\n",
    )
    failed: list = []
    with caplog.at_level(logging.WARNING, logger="ujin.jobs.app"):
        specs = _load_workflows_dir(str(tmp_path), failed=failed)
    # the good file still loads; the bad one is skipped, not fatal
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1 and failed[0]["file"].endswith("bad.yaml")
    assert "not found" in failed[0]["error"]
    assert any("skipping workflow file" in r.message for r in caplog.records)


def test_dir_loader_does_not_glob_fragment_subdir(tmp_path):
    sub = tmp_path / "fragments"
    sub.mkdir()
    _write(sub / "sink.yaml", "sinks: [{ kind: sqlite }]\n")
    _write(
        tmp_path / "main.yaml",
        "include: fragments/sink.yaml\n"
        "source: { kind: command, config: { argv: [echo, a] } }\n",
    )
    specs = _load_workflows_dir(str(tmp_path))
    # only the top-level workflow is loaded; the fragment is not a workflow
    assert [s.id for s in specs] == ["main"]
    assert [k.kind for k in specs[0].sinks] == ["sqlite"]


# ── _preload_specs (--config) parity ─────────────────────────────────────────


def test_preload_specs_honours_defaults_and_include(tmp_path):
    frag = tmp_path / "frags"
    frag.mkdir()
    _write(frag / "sink.yaml", "sinks: [{ kind: sqlite }]\n")
    cfg = _write(
        tmp_path / "jobs.yaml",
        """
defaults:
  schedule: { base: 120 }
include: frags/sink.yaml
jobs:
  - name: j1
    source: { kind: command, config: { argv: [echo, a] } }
""",
    )
    (spec,) = _preload_specs(str(cfg))
    assert spec.name == "j1"
    assert spec.schedule.base == 120
    assert [k.kind for k in spec.sinks] == ["sqlite"]


# ── regression: legacy shapes + ${VAR} parse unchanged ───────────────────────


def test_legacy_single_job_unchanged(tmp_path):
    f = _write(
        tmp_path / "page.yaml",
        """
source:
  kind: http
  config: { url: "https://example.com/" }
sinks: [{ kind: sqlite }]
schedule: { mode: adaptive, base: 300 }
""",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert spec.id == "page" and spec.name == "page"
    assert spec.source.kind == "http"
    assert spec.source.config == {"url": "https://example.com/"}
    assert [k.kind for k in spec.sinks] == ["sqlite"]
    assert spec.schedule.mode == "adaptive" and spec.schedule.base == 300


def test_legacy_multi_job_jobs_list_unchanged(tmp_path):
    f = _write(
        tmp_path / "multi.yaml",
        """
jobs:
  - name: a
    source: { kind: command, config: { argv: [echo, a] } }
  - name: b
    source: { kind: command, config: { argv: [echo, b] } }
""",
    )
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["multi-0", "multi-1"]
    assert [s.name for s in specs] == ["a", "b"]


def test_env_substitution_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("FEED_URL", "https://set.example/feed")
    monkeypatch.delenv("MISSING_URL", raising=False)
    f = _write(
        tmp_path / "env.yaml",
        """
source:
  kind: http
  config:
    url: "${FEED_URL}"
    fallback: "${MISSING_URL:-https://default.example/}"
""",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["url"] == "https://set.example/feed"
    assert spec.source.config["fallback"] == "https://default.example/"


# ── shipped example loads ────────────────────────────────────────────────────


def test_shipped_example_loads(monkeypatch):
    monkeypatch.delenv("UJIN_WORKFLOWS_DIR", raising=False)
    f = _ROOT / "examples" / "workflows" / "site-feeds.yaml"
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["site-feeds-0", "site-feeds-1"]
    for s in specs:
        assert s.source.kind == "rss"
        assert [t.kind for t in s.transforms] == ["dedupe"]   # from fragment
        assert [k.kind for k in s.sinks] == ["sqlite"]        # from fragment
        assert s.schedule.mode == "adaptive"                  # from defaults
    assert specs[0].schedule.base == 300                      # default
    assert specs[1].schedule.base == 600                      # per-job override
    assert specs[1].schedule.min == 60                        # inherited default


def test_example_fragments_subdir_not_globbed(monkeypatch):
    monkeypatch.delenv("UJIN_WORKFLOWS_DIR", raising=False)
    wf = _ROOT / "examples" / "workflows"
    specs = _load_workflows_dir(str(wf))
    ids = {s.id for s in specs}
    assert "site-feeds-0" in ids and "site-feeds-1" in ids
    assert "sqlite-sink" not in ids  # the fragment is not loaded as a workflow
