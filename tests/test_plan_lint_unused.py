"""Advisory lints for unused `vars:` defaults and unsubstituted placeholders.

Two strictly-additive advisory warnings on the ingest-plan validation path
(`ujin plan validate` and the shared `_load_ingest_plan`), consistent with the
0.45 redundant-defaults advisory and its `--strict` gating:

  * a top-level `vars:` default that no `{{ var }}` references anywhere in the
    resolved plan (dead config) — names the unused key
  * a `{{ placeholder }}` that survives matrix/for_each + vars resolution (likely
    a typo or a missing matrix column) — names the placeholder and the job id

Both are advisory by default: clean-plan and existing-plan exit codes and
non-warning output are byte-for-byte unchanged; they fail (non-zero exit) ONLY
under `--strict`. All tests are offline and deterministic; no network.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ujin.cli import main
from ujin.jobs.app import _load_ingest_plan


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _job(url: str, **extra) -> dict:
    return {
        "source": {"kind": "http", "config": {"url": url}},
        "schedule": {"mode": "once"},
        **extra,
    }


# ── primary lint: a `vars:` default no job references ─────────────────────────── #

def test_unused_vars_key_warns_advisory(tmp_path, capsys):
    # `region` is referenced; `unused_key` is declared but referenced nowhere.
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "us-east", "unused_key": "dead"},
        "jobs": [{"id": "a", **_job("https://x/{{ region }}")}],
    })
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0                       # advisory — does not fail by default
    assert "ok  a" in out
    assert "unused_key" in out           # names the unused key
    assert "region" not in out           # the referenced var is not flagged
    assert "WARN" in out


def test_unused_vars_key_strict_fails(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "us-east", "unused_key": "dead"},
        "jobs": [{"id": "a", **_job("https://x/{{ region }}")}],
    })
    rc = main(["plan", "validate", str(plan), "--strict"])
    out = capsys.readouterr().out
    assert rc != 0                       # promoted to a failure under --strict
    assert "FAIL" in out
    assert "unused_key" in out


def test_unused_vars_key_json_lists_warning(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "us-east", "unused_key": "dead"},
        "jobs": [{"id": "a", **_job("https://x/{{ region }}")}],
    })
    rc = main(["plan", "validate", str(plan), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any("unused_key" in w for w in data["warnings"])
    assert data["ok"] is True            # advisory: ok stays True without --strict


def test_matrix_supplied_var_counts_as_referenced(tmp_path, capsys):
    # `slug` is supplied by matrix rows AND referenced; `region` is a vars: default
    # referenced in the URL. No unused-vars warning despite slug not being in vars:.
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "eu"},
        "jobs": [{
            "id": "feed-{{ slug }}",
            "matrix": [{"slug": "a"}, {"slug": "b"}],
            **_job("https://x/{{ region }}/{{ slug }}"),
        }],
    })
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARN" not in out             # every vars: default is referenced


# ── clean plan: no new advisory, exits 0 with and without --strict ────────────── #

def test_clean_plan_with_vars_no_new_warning(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "us-east"},
        "jobs": [{"id": "a", **_job("https://x/{{ region }}")}],
    })
    assert main(["plan", "validate", str(plan)]) == 0
    out = capsys.readouterr().out
    assert "WARN" not in out
    assert main(["plan", "validate", str(plan), "--strict"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out


def test_no_vars_block_unchanged(tmp_path, capsys):
    # A plan with no `vars:` key emits no new advisory and exits 0 either way.
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [{"id": "a", **_job("https://x/")}]})
    assert main(["plan", "validate", str(plan)]) == 0
    assert "WARN" not in capsys.readouterr().out
    assert main(["plan", "validate", str(plan), "--strict"]) == 0


# ── secondary lint: a `{{ placeholder }}` that survives resolution ────────────── #

def test_unsubstituted_placeholder_warns_advisory(tmp_path, capsys):
    # `{{ typo }}` is set by neither vars: nor matrix, so it survives resolution.
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [{"id": "a", **_job("https://x/{{ typo }}")}]})
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0                       # advisory — does not fail by default
    assert "ok  a" in out
    assert "typo" in out                 # names the placeholder
    assert "WARN" in out


def test_unsubstituted_placeholder_strict_fails(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [{"id": "a", **_job("https://x/{{ typo }}")}]})
    rc = main(["plan", "validate", str(plan), "--strict"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "FAIL" in out
    assert "typo" in out


def test_unsubstituted_placeholder_names_job_id(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [{"id": "myjob", **_job("https://x/{{ missing }}")}]})
    main(["plan", "validate", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert any("missing" in w and "myjob" in w for w in data["warnings"])


# ── loader-level: warnings collected only when a list is given ─────────────────── #

def test_loader_collects_unused_vars_warning(tmp_path):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "us", "ghost": "x"},
        "jobs": [{"id": "a", **_job("https://x/{{ region }}")}],
    })
    warnings: list = []
    specs = _load_ingest_plan(plan, warnings=warnings)
    assert [s.id for s in specs] == ["a"]        # job still loads (advisory only)
    assert any("ghost" in w for w in warnings)


def test_loader_no_warnings_arg_is_unchanged(tmp_path):
    # Without a warnings list the loader resolves jobs identically — no exceptions
    # from the advisory path even when a dead var / stray placeholder is present.
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "us", "ghost": "x"},
        "jobs": [{"id": "a", **_job("https://x/{{ region }}/{{ typo }}")}],
    })
    specs = _load_ingest_plan(plan)
    assert [s.id for s in specs] == ["a"]
    # the unsubstituted placeholder is still left verbatim (permissive)
    assert specs[0].source.config["url"] == "https://x/us/{{ typo }}"
