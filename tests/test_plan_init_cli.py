"""ujin plan init — offline, deterministic tests.

Covers: default path (ingest-plan.yaml in cwd), custom path, existing-file safety
(no --force), --force overwrite, -f short flag, unwritable-path error, the generated
file passes plan validate (including --strict), plan render resolves at least one
job, and the template demonstrates all required building blocks.
All tests use tmp_path; no network.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ujin.cli import main


# ── default path ───────────────────────────────────────────────────────────── #

def test_default_path_creates_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["plan", "init"])
    assert rc == 0
    assert (tmp_path / "ingest-plan.yaml").exists()


def test_default_path_exits_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["plan", "init"])
    assert rc == 0


# ── custom path ────────────────────────────────────────────────────────────── #

def test_custom_path_creates_file(tmp_path):
    dest = tmp_path / "my-plan.yaml"
    rc = main(["plan", "init", str(dest)])
    assert rc == 0
    assert dest.exists()


def test_custom_path_file_is_nonempty(tmp_path):
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    assert dest.stat().st_size > 0


# ── stdout messages ────────────────────────────────────────────────────────── #

def test_stdout_names_destination(tmp_path, capsys):
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    out = capsys.readouterr().out
    assert "wrote" in out
    assert str(dest) in out


def test_stdout_hints_validate(tmp_path, capsys):
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    out = capsys.readouterr().out
    assert "plan validate" in out


def test_stdout_hints_render(tmp_path, capsys):
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    out = capsys.readouterr().out
    assert "plan render" in out


# ── existing file without --force ─────────────────────────────────────────── #

def test_existing_file_no_force_exits_nonzero(tmp_path):
    dest = tmp_path / "plan.yaml"
    dest.write_text("existing\n", encoding="utf-8")
    rc = main(["plan", "init", str(dest)])
    assert rc != 0


def test_existing_file_no_force_clean_message(tmp_path, capsys):
    dest = tmp_path / "plan.yaml"
    dest.write_text("existing\n", encoding="utf-8")
    main(["plan", "init", str(dest)])
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert str(dest) in err
    assert "--force" in err


def test_existing_file_no_force_not_overwritten(tmp_path):
    dest = tmp_path / "plan.yaml"
    dest.write_text("original content\n", encoding="utf-8")
    main(["plan", "init", str(dest)])
    assert dest.read_text(encoding="utf-8") == "original content\n"


def test_existing_file_no_force_no_traceback(tmp_path, capsys):
    dest = tmp_path / "plan.yaml"
    dest.write_text("x\n", encoding="utf-8")
    main(["plan", "init", str(dest)])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Exception" not in err


# ── --force / -f overwrite ─────────────────────────────────────────────────── #

def test_force_overwrites_existing(tmp_path):
    dest = tmp_path / "plan.yaml"
    dest.write_text("original\n", encoding="utf-8")
    rc = main(["plan", "init", str(dest), "--force"])
    assert rc == 0
    assert "defaults:" in dest.read_text(encoding="utf-8")


def test_force_short_flag_overwrites(tmp_path):
    dest = tmp_path / "plan.yaml"
    dest.write_text("original\n", encoding="utf-8")
    rc = main(["plan", "init", str(dest), "-f"])
    assert rc == 0
    assert "defaults:" in dest.read_text(encoding="utf-8")


# ── unwritable path ────────────────────────────────────────────────────────── #

@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_unwritable_path_exits_nonzero(tmp_path, capsys):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    dest = locked / "plan.yaml"
    try:
        rc = main(["plan", "init", str(dest)])
        assert rc != 0
    finally:
        locked.chmod(stat.S_IRWXU)


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_unwritable_path_clean_message(tmp_path, capsys):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    dest = locked / "plan.yaml"
    try:
        main(["plan", "init", str(dest)])
        err = capsys.readouterr().err
        assert "ujin:" in err
        assert "Traceback" not in err
    finally:
        locked.chmod(stat.S_IRWXU)


# ── generated file: plan validate ─────────────────────────────────────────── #

def test_generated_file_passes_plan_validate(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    capsys.readouterr()
    rc = main(["plan", "validate", str(dest)])
    assert rc == 0


def test_generated_file_passes_plan_validate_strict(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    capsys.readouterr()
    rc = main(["plan", "validate", str(dest), "--strict"])
    assert rc == 0


def test_generated_file_validate_reports_no_failures(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    capsys.readouterr()
    main(["plan", "validate", str(dest), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["failed"] == []
    assert data["warnings"] == []


# ── generated file: plan render ───────────────────────────────────────────── #

def test_generated_file_renders_exits_zero(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    capsys.readouterr()
    rc = main(["plan", "render", str(dest)])
    assert rc == 0


def test_generated_file_renders_at_least_one_job(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    capsys.readouterr()
    main(["plan", "render", str(dest)])
    out = capsys.readouterr().out
    assert "homepage" in out or "feed-" in out


def test_generated_file_renders_three_jobs_via_json(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    capsys.readouterr()
    rc = main(["plan", "render", str(dest), "--json"])
    assert rc == 0
    jobs = json.loads(capsys.readouterr().out)
    ids = {j["id"] for j in jobs}
    assert "homepage" in ids
    assert "feed-tech" in ids
    assert "feed-science" in ids


def test_generated_file_env_var_default_resolved(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    capsys.readouterr()
    main(["plan", "render", str(dest)])
    out = capsys.readouterr().out
    # ${BASE_URL:-https://example.com} should expand to the default
    assert "example.com" in out


# ── generated file: template features ─────────────────────────────────────── #

def test_generated_file_has_defaults_block(tmp_path):
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    content = dest.read_text(encoding="utf-8")
    assert "defaults:" in content


def test_generated_file_has_matrix(tmp_path):
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    content = dest.read_text(encoding="utf-8")
    assert "matrix:" in content


def test_generated_file_has_env_var_substitution(tmp_path):
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    content = dest.read_text(encoding="utf-8")
    assert "${" in content


def test_generated_file_has_profiles_or_include(tmp_path):
    dest = tmp_path / "plan.yaml"
    main(["plan", "init", str(dest)])
    content = dest.read_text(encoding="utf-8")
    assert "profiles:" in content or "include:" in content
