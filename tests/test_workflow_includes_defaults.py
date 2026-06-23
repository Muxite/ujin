"""Workflow ``defaults:`` deep-merge + ``include:``/``use:`` reusable fragments.

All offline and deterministic — these exercise the pure parsing helpers in
:mod:`ujin.jobs.app` directly (no FastAPI, no network), so they run even without
the jobs service extra installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")

from ujin.jobs.app import (  # noqa: E402
    WorkflowIncludeError,
    _load_workflows_dir,
    _specs_from_workflow_file,
)


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _effective(spec) -> dict:
    """A spec's content with the volatile/identity fields stripped off."""
    d = spec.to_dict()
    for k in ("id", "name", "created_at"):
        d.pop(k, None)
    return d


# ── defaults: deep-merge precedence ──────────────────────────────────────────


def test_defaults_deep_merge_precedence(tmp_path):
    _write(
        tmp_path / "main.yaml",
        """
defaults:
  source:
    kind: http
    config:
      timeout: 30
      retries: 3
  schedule:
    mode: adaptive
    base: 300
  transforms:
    - kind: dedupe
      config: {key: id}
  sinks:
    - kind: sqlite
jobs:
  - name: a
    source:
      config:
        url: "https://a.test"
        retries: 5
    schedule:
      base: 60
  - name: b
    source:
      config:
        url: "https://b.test"
    transforms:
      - kind: limit
        config: {n: 10}
    sinks: []
""",
    )
    specs = _specs_from_workflow_file(tmp_path / "main.yaml")
    a, b = specs

    # filename-stem ids stay deterministic for a multi-job file
    assert [s.id for s in specs] == ["main-0", "main-1"]
    assert [s.name for s in specs] == ["a", "b"]

    # job `a`: nested source/schedule maps deep-merge; per-job keys win
    assert a.source.kind == "http"  # inherited from defaults
    assert a.source.config == {"timeout": 30, "retries": 5, "url": "https://a.test"}
    assert a.schedule.mode == "adaptive"  # inherited
    assert a.schedule.base == 60.0  # overridden
    # job `a` omits transforms/sinks -> default lists are inherited wholesale
    assert [t.kind for t in a.transforms] == ["dedupe"]
    assert [s.kind for s in a.sinks] == ["sqlite"]

    # job `b`: present list keys REPLACE the defaults (no concatenation)
    assert b.source.config == {"timeout": 30, "retries": 3, "url": "https://b.test"}
    assert [t.kind for t in b.transforms] == ["limit"]
    assert b.sinks == []


# ── include:/use: fragments resolve to the same effective spec as inlining ────


def _seed_fragments(tmp_path):
    _write(
        tmp_path / "fragments" / "webhook-sink.yaml",
        "kind: webhook\nconfig:\n  url: \"http://backend.test/ingest\"\n",
    )
    _write(
        tmp_path / "fragments" / "hourly.yaml",
        "mode: cron\ncron: \"0 * * * *\"\n",
    )
    _write(
        tmp_path / "fragments" / "clean.yaml",
        """
- kind: select
  config: {fields: [a, b]}
- kind: dedupe
  config: {key: a}
""",
    )


def test_include_subsections_equal_inlined(tmp_path):
    _seed_fragments(tmp_path)
    _write(
        tmp_path / "included.yaml",
        """
source:
  kind: http
  config: {url: "https://x.test"}
transforms:
  - include: fragments/clean.yaml       # a list fragment -> spliced into the pipeline
  - kind: limit
    config: {n: 5}
sinks:
  - use: fragments/webhook-sink.yaml     # `use:` is an alias for `include:`
  - kind: sqlite
schedule:
  include: fragments/hourly.yaml
""",
    )
    _write(
        tmp_path / "inlined.yaml",
        """
source:
  kind: http
  config: {url: "https://x.test"}
transforms:
  - kind: select
    config: {fields: [a, b]}
  - kind: dedupe
    config: {key: a}
  - kind: limit
    config: {n: 5}
sinks:
  - kind: webhook
    config: {url: "http://backend.test/ingest"}
  - kind: sqlite
schedule:
  mode: cron
  cron: "0 * * * *"
""",
    )
    (inc,) = _specs_from_workflow_file(tmp_path / "included.yaml")
    (lin,) = _specs_from_workflow_file(tmp_path / "inlined.yaml")
    assert _effective(inc) == _effective(lin)
    # spot-check the spliced pipeline order survives
    assert [t.kind for t in inc.transforms] == ["select", "dedupe", "limit"]
    assert inc.schedule.cron == "0 * * * *"


def test_whole_job_include_with_overrides(tmp_path):
    _write(
        tmp_path / "fragments" / "base-job.yaml",
        """
source:
  kind: http
  config: {url: "https://base.test", timeout: 30}
schedule:
  mode: adaptive
  base: 300
sinks:
  - kind: sqlite
""",
    )
    _write(
        tmp_path / "job.yaml",
        """
include: fragments/base-job.yaml
schedule:
  base: 60
""",
    )
    (spec,) = _specs_from_workflow_file(tmp_path / "job.yaml")
    assert spec.source.config == {"url": "https://base.test", "timeout": 30}
    assert spec.schedule.mode == "adaptive"  # kept from the fragment
    assert spec.schedule.base == 60.0  # local override wins
    assert [s.kind for s in spec.sinks] == ["sqlite"]
    assert spec.id == "job"  # stem id unaffected by the include


# ── additive no-op path: no defaults, no include ─────────────────────────────


def test_no_defaults_no_include_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://env.test")
    monkeypatch.delenv("TOKEN", raising=False)
    _write(
        tmp_path / "plain.yaml",
        """
source:
  kind: http
  config:
    url: "${BASE_URL}/items"
    token: "${TOKEN:-anon}"
schedule:
  mode: once
""",
    )
    (spec,) = _specs_from_workflow_file(tmp_path / "plain.yaml")
    # id/name still derive from the stem; ${VAR}/${VAR:-default} unchanged
    assert spec.id == "plain"
    assert spec.name == "plain"
    assert spec.source.config == {"url": "https://env.test/items", "token": "anon"}
    assert spec.schedule.mode == "once"


# ── guard rails: missing / cyclic includes ───────────────────────────────────


def test_missing_include_raises_actionable_error(tmp_path):
    _write(tmp_path / "bad.yaml", "include: fragments/nope.yaml\n")
    with pytest.raises(WorkflowIncludeError) as exc:
        _specs_from_workflow_file(tmp_path / "bad.yaml")
    assert "not found" in str(exc.value)
    assert "nope.yaml" in str(exc.value)


def test_missing_include_lands_in_failed_without_aborting(tmp_path):
    _write(
        tmp_path / "good.yaml",
        "source:\n  kind: http\n  config: {url: \"https://ok.test\"}\n",
    )
    _write(tmp_path / "bad.yaml", "include: fragments/nope.yaml\n")
    failed: list = []
    specs = _load_workflows_dir(str(tmp_path), failed=failed)

    # the healthy workflow still loads; the broken one is reported, not fatal
    assert [s.id for s in specs] == ["good"]
    assert [f["id"] for f in failed] == ["bad"]
    assert "not found" in failed[0]["error"]


def test_cyclic_include_detected(tmp_path):
    # main -> cyc/one -> cyc/two -> cyc/one  (fragments in a subdir so the
    # top-level scan does not pick them up as standalone workflows)
    _write(tmp_path / "main.yaml", "include: cyc/one.yaml\n")
    _write(tmp_path / "cyc" / "one.yaml", "include: two.yaml\n")
    _write(tmp_path / "cyc" / "two.yaml", "include: one.yaml\n")

    with pytest.raises(WorkflowIncludeError) as exc:
        _specs_from_workflow_file(tmp_path / "main.yaml")
    assert "cyclic" in str(exc.value)

    failed: list = []
    assert _load_workflows_dir(str(tmp_path), failed=failed) == []
    assert [f["id"] for f in failed] == ["main"]
    assert "cyclic" in failed[0]["error"]


def test_multiple_includes_apply_left_to_right(tmp_path):
    _write(tmp_path / "fragments" / "base.yaml", "kind: http\nconfig: {timeout: 10}\n")
    _write(tmp_path / "fragments" / "over.yaml", "config: {timeout: 99, retries: 2}\n")
    _write(
        tmp_path / "job.yaml",
        """
source:
  include: [fragments/base.yaml, fragments/over.yaml]   # later wins
schedule: { mode: once }
""",
    )
    (spec,) = _specs_from_workflow_file(tmp_path / "job.yaml")
    assert spec.source.kind == "http"
    assert spec.source.config == {"timeout": 99, "retries": 2}


def test_include_with_bad_type_raises(tmp_path):
    _write(tmp_path / "job.yaml", "source:\n  include: 123\n")
    with pytest.raises(WorkflowIncludeError) as exc:
        _specs_from_workflow_file(tmp_path / "job.yaml")
    assert "path or list of paths" in str(exc.value)


def test_absolute_include_path(tmp_path):
    frag = tmp_path / "shared" / "sink.yaml"
    _write(frag, "kind: sqlite\n")
    _write(
        tmp_path / "job.yaml",
        f"source:\n  kind: http\n  config: {{url: x}}\nsinks:\n  - include: {frag}\n",
    )
    (spec,) = _specs_from_workflow_file(tmp_path / "job.yaml")
    assert [s.kind for s in spec.sinks] == ["sqlite"]

    # a missing absolute path is still an actionable error
    _write(tmp_path / "bad.yaml", "sinks:\n  - include: /no/such/frag.yaml\n")
    with pytest.raises(WorkflowIncludeError):
        _specs_from_workflow_file(tmp_path / "bad.yaml")


def test_keys_alongside_a_list_fragment_is_an_error(tmp_path):
    _write(
        tmp_path / "fragments" / "pipe.yaml",
        "- kind: select\n  config: {fields: [a]}\n",
    )
    _write(
        tmp_path / "job.yaml",
        """
source: { kind: http, config: {url: x} }
transforms:
  - include: fragments/pipe.yaml      # a list fragment
    kind: limit                        # ...cannot also carry override keys
""",
    )
    with pytest.raises(WorkflowIncludeError) as exc:
        _specs_from_workflow_file(tmp_path / "job.yaml")
    assert "list fragment" in str(exc.value)


def test_env_workflows_dir_resolves_fragments(tmp_path, monkeypatch):
    # A fragment referenced by a bare name resolves via $UJIN_WORKFLOWS_DIR even
    # when the workflow file lives elsewhere.
    shared = tmp_path / "shared"
    _write(shared / "common-sink.yaml", "kind: sqlite\n")
    monkeypatch.setenv("UJIN_WORKFLOWS_DIR", str(shared))

    wf = tmp_path / "elsewhere"
    _write(
        wf / "job.yaml",
        """
source:
  kind: http
  config: {url: "https://y.test"}
sinks:
  - include: common-sink.yaml
""",
    )
    (spec,) = _specs_from_workflow_file(wf / "job.yaml")
    assert [s.kind for s in spec.sinks] == ["sqlite"]
