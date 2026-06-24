"""ujin plan env — offline, deterministic tests.

Covers the strictly-additive ``ujin plan env`` subcommand that reports the
``${VAR}`` environment variables a plan/workflow file references:

  * plain (human) output and --json output;
  * defaults declared via ``${VAR:-default}``;
  * required vars declared via ``${VAR:?message}``;
  * set-vs-unset detection (process env + --env-file);
  * include:/use: fragment references are walked;
  * a plan with no ${VAR} references reports an empty set;
  * a workflows directory; a missing path; --env-file errors.

All tests use tmp_path and monkeypatch; no network, no real env mutation.
"""
from __future__ import annotations

import json

import pytest

from ujin.cli import main
from ujin.jobs.app import collect_plan_env_refs


# ── helpers ─────────────────────────────────────────────────────────────────── #

def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plan_with_vars(path) -> None:
    """A plan referencing a plain var, a defaulted var, and a required var."""
    _write(path, (
        "- id: papers\n"
        "  source:\n"
        "    kind: api\n"
        "    config:\n"
        '      url: "https://api.example.com/works?token=${API_TOKEN}"\n'
        '      rows: "${ROWS:-50}"\n'
        '      auth: "Bearer ${SECRET:?set SECRET to the ingest token}"\n'
    ))


# ── unit: collect_plan_env_refs ─────────────────────────────────────────────── #

def test_collect_refs_records_default_required_and_plain(tmp_path):
    plan = tmp_path / "plan.yaml"
    _plan_with_vars(plan)
    refs = collect_plan_env_refs(plan)
    assert list(refs) == ["API_TOKEN", "ROWS", "SECRET"]
    assert refs["API_TOKEN"] == {"default": None, "required": False}
    assert refs["ROWS"] == {"default": "50", "required": False}
    assert refs["SECRET"] == {"default": None, "required": True}


def test_collect_refs_empty_for_plan_without_vars(tmp_path):
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "- id: plain\n"
        "  source: { kind: http, config: { url: \"https://example.com/\" } }\n"
    ))
    assert collect_plan_env_refs(plan) == {}


def test_collect_refs_walks_include_fragments(tmp_path):
    _write(tmp_path / "fragments" / "sink.yaml", (
        "kind: webhook\n"
        'config: { url: "${WEBHOOK_URL:?set the webhook url}" }\n'
    ))
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "defaults:\n"
        "  sinks:\n"
        "    - include: fragments/sink.yaml\n"
        "jobs:\n"
        "  - id: papers\n"
        '    source: { kind: api, config: { url: "https://x/${API_TOKEN}" } }\n'
    ))
    refs = collect_plan_env_refs(plan)
    # The fragment's ${WEBHOOK_URL:?...} is collected and marked required.
    assert refs["WEBHOOK_URL"] == {"default": None, "required": True}
    assert "API_TOKEN" in refs


def test_collect_refs_first_default_wins(tmp_path):
    """A var referenced twice keeps the first declared ${VAR:-default}."""
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "- id: a\n"
        '  source: { kind: http, config: { a: "${V:-first}", b: "${V:-second}" } }\n'
    ))
    assert collect_plan_env_refs(plan)["V"]["default"] == "first"


def test_collect_refs_walks_list_valued_include(tmp_path):
    """An include: whose value is a list of fragment paths is fully walked."""
    _write(tmp_path / "frag-a.yaml", 'kind: a\nconfig: { url: "${A_URL}" }\n')
    _write(tmp_path / "frag-b.yaml", 'kind: b\nconfig: { url: "${B_URL:-x}" }\n')
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "- id: j\n"
        "  source: { kind: api, config: { url: \"https://x/\" } }\n"
        "  include: [frag-a.yaml, frag-b.yaml]\n"
    ))
    refs = collect_plan_env_refs(plan)
    assert "A_URL" in refs and refs["B_URL"]["default"] == "x"


def test_collect_refs_cyclic_include_is_lenient(tmp_path):
    """A cyclic include terminates (seen-guard) and still reports both files' vars."""
    _write(tmp_path / "a.yaml", 'config: { x: "${A_VAR}" }\ninclude: b.yaml\n')
    _write(tmp_path / "b.yaml", 'config: { y: "${B_VAR}" }\ninclude: a.yaml\n')
    refs = collect_plan_env_refs(tmp_path / "a.yaml")
    assert set(refs) == {"A_VAR", "B_VAR"}


def test_collect_refs_unparseable_file_still_scans_text(tmp_path):
    """An unparseable YAML file still yields its ${VAR} refs (regex scans raw text)."""
    plan = tmp_path / "plan.yaml"
    # Unbalanced flow mapping -> YAMLError on parse, but ${BROKEN_VAR} is in the text.
    _write(plan, 'source: { kind: http, config: { url: "${BROKEN_VAR}" \n')
    assert collect_plan_env_refs(plan) == {"BROKEN_VAR": {"default": None, "required": False}}


def test_collect_refs_missing_fragment_is_skipped(tmp_path):
    """An unresolvable include: fragment is skipped, not raised."""
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "- id: j\n"
        "  source: { kind: http, config: { url: \"${ONLY_VAR}\" } }\n"
        "  include: does-not-exist.yaml\n"
    ))
    assert collect_plan_env_refs(plan) == {"ONLY_VAR": {"default": None, "required": False}}


def test_collect_refs_ignores_non_path_include_value(tmp_path):
    """A malformed include: (a scalar, not a path/list) is ignored, not raised."""
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "- id: j\n"
        "  include: 123\n"
        "  source: { kind: http, config: { url: \"${ONLY_VAR}\" } }\n"
    ))
    assert collect_plan_env_refs(plan) == {"ONLY_VAR": {"default": None, "required": False}}


def test_collect_refs_ignores_matrix_filter_include_lists(tmp_path):
    """A matrix `include:` filter (variable maps) is not mistaken for a fragment."""
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "- id: feed-{{ slug }}\n"
        "  matrix:\n"
        "    include:\n"
        "      - { slug: extra }\n"
        '  source: { kind: api, config: { url: "https://x/${API_TOKEN}?s={{ slug }}" } }\n'
    ))
    # Resolves without trying to open `{slug: extra}` as a fragment file.
    refs = collect_plan_env_refs(plan)
    assert list(refs) == ["API_TOKEN"]


# ── CLI: plain (human) output ───────────────────────────────────────────────── #

def test_plain_output_lists_vars(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret")
    monkeypatch.delenv("ROWS", raising=False)
    monkeypatch.delenv("SECRET", raising=False)
    plan = tmp_path / "plan.yaml"
    _plan_with_vars(plan)

    rc = main(["plan", "env", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "3 variables referenced" in out
    # set-vs-unset reflects the process environment
    assert "API_TOKEN" in out and "set" in out
    # defaulted var shows its default; required var is labelled required
    assert "default=50" in out
    assert "required" in out
    # one body line per variable plus the header
    assert out.count("\n") == 4


def test_plain_output_required_vs_optional_and_set_state(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ROWS", "100")
    monkeypatch.setenv("SECRET", "tok")
    plan = tmp_path / "plan.yaml"
    _plan_with_vars(plan)

    main(["plan", "env", str(plan)])
    lines = {ln.split()[0]: ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  ")}
    assert "optional" in lines["API_TOKEN"] and "unset" in lines["API_TOKEN"]
    assert "optional" in lines["ROWS"] and "set" in lines["ROWS"]
    assert "required" in lines["SECRET"] and "set" in lines["SECRET"]


def test_plain_output_empty_set(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "- id: plain\n"
        "  source: { kind: http, config: { url: \"https://example.com/\" } }\n"
    ))
    rc = main(["plan", "env", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "no environment variables referenced"


# ── CLI: --json output ──────────────────────────────────────────────────────── #

def test_json_output_shape(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret")
    monkeypatch.delenv("ROWS", raising=False)
    monkeypatch.delenv("SECRET", raising=False)
    plan = tmp_path / "plan.yaml"
    _plan_with_vars(plan)

    rc = main(["plan", "env", str(plan), "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["path"] == str(plan)
    by_name = {v["name"]: v for v in doc["vars"]}
    assert by_name["API_TOKEN"] == {
        "name": "API_TOKEN", "default": None, "required": False, "set": True}
    assert by_name["ROWS"] == {
        "name": "ROWS", "default": "50", "required": False, "set": False}
    assert by_name["SECRET"] == {
        "name": "SECRET", "default": None, "required": True, "set": False}


def test_json_output_empty_vars(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, (
        "- id: plain\n"
        "  source: { kind: http, config: { url: \"https://example.com/\" } }\n"
    ))
    rc = main(["plan", "env", str(plan), "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["vars"] == []


# ── CLI: --env-file feeds the set/unset check ───────────────────────────────── #

def test_env_file_counts_as_set(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("ROWS", raising=False)
    monkeypatch.delenv("SECRET", raising=False)
    plan = tmp_path / "plan.yaml"
    _plan_with_vars(plan)
    env_file = tmp_path / ".env"
    env_file.write_text("ROWS=200\nSECRET=tok\n", encoding="utf-8")

    rc = main(["plan", "env", str(plan), "--json", "--env-file", str(env_file)])
    assert rc == 0
    by_name = {v["name"]: v for v in json.loads(capsys.readouterr().out)["vars"]}
    assert by_name["ROWS"]["set"] is True
    assert by_name["SECRET"]["set"] is True
    assert by_name["API_TOKEN"]["set"] is False


def test_env_file_process_env_wins(tmp_path, capsys, monkeypatch):
    # A value already in the process env is not overridden by the file, but the
    # variable still counts as set either way; this just exercises precedence.
    monkeypatch.setenv("ROWS", "from-process")
    plan = tmp_path / "plan.yaml"
    _plan_with_vars(plan)
    env_file = tmp_path / ".env"
    env_file.write_text("ROWS=from-file\n", encoding="utf-8")

    main(["plan", "env", str(plan), "--json", "--env-file", str(env_file)])
    by_name = {v["name"]: v for v in json.loads(capsys.readouterr().out)["vars"]}
    assert by_name["ROWS"]["set"] is True


def test_env_file_missing_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _plan_with_vars(plan)
    rc = main(["plan", "env", str(plan), "--env-file", str(tmp_path / "nope.env")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ujin:" in err


# ── CLI: directory mode and missing path ────────────────────────────────────── #

def test_workflows_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("A_VAR", raising=False)
    monkeypatch.delenv("B_VAR", raising=False)
    _write(tmp_path / "a.yaml",
           "source: { kind: http, config: { url: \"https://x/${A_VAR}\" } }\n")
    _write(tmp_path / "b.yaml",
           "source: { kind: http, config: { url: \"https://y/${B_VAR:-z}\" } }\n")
    rc = main(["plan", "env", str(tmp_path), "--json"])
    assert rc == 0
    names = [v["name"] for v in json.loads(capsys.readouterr().out)["vars"]]
    assert set(names) == {"A_VAR", "B_VAR"}


def test_unreadable_path_exits_nonzero(tmp_path, capsys):
    """An I/O error reading a matched file exits 1 with a clean ujin: … message.

    A directory whose name ends in ``.yaml`` is matched by the workflows-dir glob but
    cannot be ``read_text``-ed (IsADirectoryError), exercising the OSError handler.
    """
    (tmp_path / "trap.yaml").mkdir()
    rc = main(["plan", "env", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ujin: cannot read" in err


def test_missing_path_human(tmp_path, capsys):
    rc = main(["plan", "env", str(tmp_path / "missing.yaml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ujin: path not found" in err


def test_missing_path_json(tmp_path, capsys):
    rc = main(["plan", "env", str(tmp_path / "missing.yaml"), "--json"])
    out = capsys.readouterr().out
    assert rc == 1
    doc = json.loads(out)
    assert doc["vars"] == []
    assert "path not found" in doc["error"]
