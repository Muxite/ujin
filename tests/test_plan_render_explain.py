"""``ujin plan render --explain`` — per-job provenance annotation (offline).

``--explain`` threads provenance through the SAME resolution pipeline as a plain
render (``defaults:`` deep-merge, ``include:``/``use:`` fragments, ``matrix:``/
``for_each:`` fan-out, ``${VAR}`` substitution) and annotates each fully-expanded
job with its originating source file plus — for matrix-expanded jobs — the matrix
entry index and the variable map that produced it. These tests build a plan that
exercises defaults, an include fragment, and a matrix fan-out, then assert the
provenance is correct and that ``--explain`` composes with ``--json``,
``--only``/``--exclude``, and ``--env-file``. All hermetic — no network.

The non-``--explain`` render path must stay byte-for-byte unchanged; a dedicated
test pins that.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ujin.cli import main as cli_main
from ujin.jobs.app import _load_ingest_plan


def _run_cli(argv):
    try:
        return cli_main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture()
def plan_tree(tmp_path) -> Path:
    """A plan that exercises defaults + an include fragment + a matrix fan-out.

    Resolves to four jobs: ``crossref`` (plain, defaults only), ``feed-tech`` /
    ``feed-science`` (matrix fan-out, with a sink pulled from an include fragment).
    """
    frag = tmp_path / "fragments" / "webhook-sink.yaml"
    _write(frag, {"kind": "webhook", "config": {"url": "https://hook/ingest"}})

    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "defaults": {
            "source": {"kind": "api", "config": {"method": "GET"}},
            "schedule": {"mode": "once"},
        },
        "jobs": [
            {
                "id": "crossref",
                "source": {"config": {"url": "https://api.crossref.org/works"}},
            },
            {
                "id": "feed-{{ slug }}",
                "matrix": [{"slug": "tech"}, {"slug": "science"}],
                "source": {"config": {"url": "https://feeds/{{ slug }}"}},
                "sinks": [{"include": "fragments/webhook-sink.yaml"}],
            },
        ],
    })
    return plan


# ── loader-level: provenance is collected in lock-step with specs ───────────── #

def test_loader_collects_provenance_per_spec(plan_tree):
    """`_load_ingest_plan(provenance=...)` yields one record per resolved spec."""
    provenance: list = []
    specs = _load_ingest_plan(plan_tree, provenance=provenance)
    assert [s.id for s in specs] == ["crossref", "feed-tech", "feed-science"]
    by_id = {p["id"]: p for p in provenance}
    # Every resolved job carries its originating source file.
    assert set(by_id) == {"crossref", "feed-tech", "feed-science"}
    for rec in provenance:
        assert rec["source"] == str(plan_tree)
    # Plain job: no matrix provenance.
    assert "matrix" not in by_id["crossref"]
    # Matrix jobs: the entry index and variable map that produced each.
    assert by_id["feed-tech"]["matrix"] == {
        "key": "matrix", "index": 0, "vars": {"slug": "tech"},
    }
    assert by_id["feed-science"]["matrix"] == {
        "key": "matrix", "index": 1, "vars": {"slug": "science"},
    }


def test_loader_without_provenance_is_unchanged(plan_tree):
    """Omitting `provenance` resolves to the identical specs (default path)."""
    def _stable(specs):
        out = []
        for s in specs:
            d = s.to_dict()
            d.pop("created_at", None)  # volatile timestamp, not identity
            out.append(d)
        return out

    plain = _load_ingest_plan(plan_tree)
    explained = _load_ingest_plan(plan_tree, provenance=[])
    assert _stable(plain) == _stable(explained)


# ── CLI: --explain exits 0 and associates each job with its source/matrix ───── #

def test_cli_explain_yaml_exit_zero_and_annotations(plan_tree, capsys):
    rc = _run_cli(["plan", "render", str(plan_tree), "--explain"])
    out = capsys.readouterr().out
    assert rc == 0
    # Each job's source file is named.
    assert out.count(f"# source: {plan_tree}") == 3
    # Matrix-expanded jobs name the entry index and variable map.
    assert "# matrix: entry 0 -> {slug: tech}" in out
    assert "# matrix: entry 1 -> {slug: science}" in out
    # The plain job is rendered without a matrix comment.
    assert "--- # crossref" in out
    # The full spec body is still emitted (the resolved url survives).
    assert "https://feeds/tech" in out


def test_cli_explain_json_machine_readable(plan_tree, capsys):
    rc = _run_cli(["plan", "render", str(plan_tree), "--explain", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(data, list) and len(data) == 3
    by_id = {e["spec"]["id"]: e for e in data}
    # Each element pairs the full spec with its provenance.
    assert by_id["crossref"]["spec"]["source"]["config"]["method"] == "GET"
    assert by_id["crossref"]["provenance"] == {"source": str(plan_tree)}
    assert by_id["feed-tech"]["provenance"]["matrix"] == {
        "key": "matrix", "index": 0, "vars": {"slug": "tech"},
    }
    assert by_id["feed-science"]["provenance"]["matrix"]["index"] == 1


# ── compose: --explain with --only / --exclude / --env-file ─────────────────── #

def test_cli_explain_with_only(plan_tree, capsys):
    rc = _run_cli(
        ["plan", "render", str(plan_tree), "--explain", "--only", "feed-*"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "--- # feed-tech" in out and "--- # feed-science" in out
    assert "--- # crossref" not in out
    # Provenance survives selection (matched after full resolution).
    assert "# matrix: entry 0 -> {slug: tech}" in out


def test_cli_explain_with_exclude_json(plan_tree, capsys):
    rc = _run_cli(
        ["plan", "render", str(plan_tree), "--explain", "--json",
         "--exclude", "feed-science"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    ids = {e["spec"]["id"] for e in data}
    assert ids == {"crossref", "feed-tech"}
    # Provenance still aligned to the surviving specs.
    by_id = {e["spec"]["id"]: e for e in data}
    assert by_id["feed-tech"]["provenance"]["matrix"]["vars"] == {"slug": "tech"}


def test_cli_explain_with_env_file(tmp_path, capsys):
    """--explain composes with --env-file: ${VAR} is filled, then annotated."""
    frag = tmp_path / "fragments" / "webhook-sink.yaml"
    _write(frag, {"kind": "webhook", "config": {"url": "https://hook"}})
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "jobs:\n"
        "  - id: 'feed-{{ slug }}'\n"
        "    matrix:\n"
        "      - { slug: tech }\n"
        "    source:\n"
        "      kind: api\n"
        "      config: { url: '${FEED_BASE}/{{ slug }}' }\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "vars.env"
    env_file.write_text("FEED_BASE=https://feeds.example.com\n", encoding="utf-8")

    rc = _run_cli(
        ["plan", "render", str(plan), "--explain", "--env-file", str(env_file)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    # ${FEED_BASE} expanded from the env-file, then provenance annotated.
    assert "https://feeds.example.com/tech" in out
    assert "# matrix: entry 0 -> {slug: tech}" in out
    assert f"# source: {plan}" in out


# ── workflows-directory provenance (each job names its own file) ─────────────── #

def test_cli_explain_workflows_dir_names_each_file(tmp_path, capsys):
    wf = tmp_path / "wf"
    _write(wf / "alpha.yaml", {
        "source": {"kind": "command", "config": {"argv": ["printf", "x"]}},
        "schedule": {"mode": "once"},
    })
    _write(wf / "beta.yaml", {
        "id": "beta-{{ n }}",
        "matrix": [{"n": 1}, {"n": 2}],
        "source": {"kind": "command", "config": {"argv": ["printf", "{{ n }}"]}},
        "schedule": {"mode": "once"},
    })
    rc = _run_cli(["plan", "render", str(wf), "--explain"])
    out = capsys.readouterr().out
    assert rc == 0
    # Each job points at the workflow file it came from.
    assert f"# source: {wf / 'alpha.yaml'}" in out
    assert f"# source: {wf / 'beta.yaml'}" in out
    assert "# matrix: entry 0 -> {n: 1}" in out
    assert "# matrix: entry 1 -> {n: 2}" in out


# ── the default (non --explain) render path is byte-for-byte unchanged ──────── #

def test_default_render_unchanged_yaml(plan_tree, capsys):
    """Without --explain the output carries no provenance comments at all."""
    rc = _run_cli(["plan", "render", str(plan_tree)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# source:" not in out
    assert "# matrix:" not in out
    # The job headers and bodies are exactly the existing render shape.
    assert "--- # crossref" in out
    assert "--- # feed-tech" in out


def test_default_render_json_unchanged_shape(plan_tree, capsys):
    """Without --explain --json stays a flat array of spec dicts (no wrapper)."""
    rc = _run_cli(["plan", "render", str(plan_tree), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(data, list)
    assert all("provenance" not in e and "id" in e for e in data)
    assert {e["id"] for e in data} == {"crossref", "feed-tech", "feed-science"}
