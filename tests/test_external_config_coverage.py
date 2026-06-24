"""Hermetic branch/line gap-fill for external-config and cli paths.

Closes uncovered branches in:
  ujin/jobs/app.py  — line 431 (prov.append(None) in _expand_matrix_entries)
                    — line 648->650 (False branch of `if matrix is not None:`
                      in _specs_from_workflow_file when a plain entry has no
                      matrix provenance)
  ujin/cli.py       — line 47->51 (_version() when __version__ is falsy)
                    — lines 218-220 (_cmd_jobs_serve with bad env-file)
                    — line 434->436 (_explain_comment_lines with no "source")
                    — lines 601-602 (_open_strategy_feedback_ro non-db file)

All tests are offline, deterministic, and network-free.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest
import yaml


# ── helpers ──────────────────────────────────────────────────────────────────


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


# ── ujin/jobs/app.py: line 431 + 648->650 ────────────────────────────────────


def test_expand_matrix_entries_prov_appends_none_for_plain_entry(tmp_path):
    """Line 431: prov.append(None) fires when a non-matrix entry is processed
    in the same _expand_matrix_entries call that also fans out a matrix entry.

    This branch only runs when prov is not None AND the current entry has no
    matrix key — i.e. the list has at least one matrix entry (so the early-
    return-unchanged path is skipped) and at least one plain entry.
    """
    from ujin.jobs.app import _expand_matrix_entries

    plain = {"id": "static", "source": {"kind": "http", "config": {}}}
    matrix = {
        "matrix": [{"n": "x"}, {"n": "y"}],
        "source": {"kind": "http", "config": {"url": "https://{{ n }}"}},
    }
    prov: list = []
    result = _expand_matrix_entries([plain, matrix], "stem", prov=prov)
    # plain entry passes through; matrix entry fans to 2
    assert len(result) == 3
    # prov[0] is None (plain entry — line 431); prov[1], prov[2] are records
    assert prov[0] is None
    assert prov[1] is not None and prov[1]["vars"] == {"n": "x"}
    assert prov[2] is not None and prov[2]["vars"] == {"n": "y"}


def test_specs_from_workflow_jobs_list_mixed_provenance(tmp_path):
    """Lines 648->650 (False branch): when a jobs:-list workflow contains both
    plain and matrix entries and provenance is collected, prov_rows[i] is None
    for the plain entry so `if matrix is not None:` takes the else path.

    Also covers line 431 transitively (same condition in _expand_matrix_entries).
    """
    from ujin.jobs.app import _specs_from_workflow_file

    f = tmp_path / "mix.yaml"
    _write(f, {
        "jobs": [
            {
                "id": "plain-job",
                "source": {"kind": "command", "config": {"argv": ["printf", "x"]}},
                "schedule": {"mode": "once"},
            },
            {
                "id": "fan-{{ n }}",
                "matrix": [{"n": "a"}, {"n": "b"}],
                "source": {"kind": "command", "config": {"argv": ["printf", "{{ n }}"]}},
                "schedule": {"mode": "once"},
            },
        ],
    })
    provenance: list = []
    specs = _specs_from_workflow_file(f, provenance=provenance)

    assert [s.id for s in specs] == ["plain-job", "fan-a", "fan-b"]

    by_id = {p["id"]: p for p in provenance}
    # Plain entry: no "matrix" key in provenance record (False branch at 648)
    assert "matrix" not in by_id["plain-job"]
    assert by_id["plain-job"]["source"] == str(f)

    # Matrix entries: "matrix" key is present with the right provenance
    assert by_id["fan-a"]["matrix"]["key"] == "matrix"
    assert by_id["fan-a"]["matrix"]["vars"] == {"n": "a"}
    assert by_id["fan-b"]["matrix"]["index"] == 1


# ── ujin/cli.py: line 47->51 ──────────────────────────────────────────────────


def test_version_falls_back_to_metadata_when_attr_is_none(monkeypatch):
    """Branch 47->51: if ujin.__version__ is falsy _version() falls through to
    importlib.metadata rather than returning early.

    Existing tests cover: import fails (sys.modules["ujin"]=None) and
    PackageNotFoundError. This test covers the third path: import succeeds
    but the attribute is None.
    """
    import ujin as _ujin

    monkeypatch.setattr(_ujin, "__version__", None)

    from ujin.cli import _version

    result = _version()
    # importlib.metadata returns the installed version (editable install)
    assert isinstance(result, str) and result and result != "unknown"


# ── ujin/cli.py: lines 218-220 ───────────────────────────────────────────────


def test_cmd_jobs_serve_bad_env_file_exits_early(tmp_path, capsys):
    """Lines 218-220: _cmd_jobs_serve returns 1 without calling serve() when
    --env-file points to a missing file (rc=1 from _apply_env_file).

    We drive _cmd_jobs_serve directly with a Namespace rather than going
    through `ujin jobs-serve` (which would launch uvicorn).
    """
    from ujin.cli import _cmd_jobs_serve

    args = argparse.Namespace(
        env_file=str(tmp_path / "nonexistent.env"),
        only=None,
        exclude=None,
        plan=None,
        host="localhost",
        port=8902,
        jobs=None,
        workflows=None,
    )
    rc = _cmd_jobs_serve(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_cmd_jobs_serve_bad_env_file_content_exits_early(tmp_path, capsys):
    """Lines 219-220: rc branch when env-file exists but has a parse error."""
    from ujin.cli import _cmd_jobs_serve

    bad_env = tmp_path / "bad.env"
    bad_env.write_text("NOT=VALID\nBADLINE\n", encoding="utf-8")

    args = argparse.Namespace(
        env_file=str(bad_env),
        only=None,
        exclude=None,
        plan=None,
        host="localhost",
        port=8902,
        jobs=None,
        workflows=None,
    )
    rc = _cmd_jobs_serve(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err


def test_cmd_jobs_serve_valid_env_file_reaches_serve(tmp_path, monkeypatch):
    """Branch 219->222: when env-file is valid (rc=0), execution continues past
    the `if rc:` check and reaches the serve() call.

    We mock serve() to a no-op so uvicorn never actually starts.
    """
    from ujin.cli import _cmd_jobs_serve

    served: list = []

    def _fake_serve(**kw):
        served.append(kw)

    monkeypatch.setattr("ujin.jobs.app.serve", _fake_serve)

    env_file = tmp_path / "good.env"
    env_file.write_text("UJIN_TEST_EV_SERVE=hello\n", encoding="utf-8")
    monkeypatch.delenv("UJIN_TEST_EV_SERVE", raising=False)

    args = argparse.Namespace(
        env_file=str(env_file),
        only=None,
        exclude=None,
        plan=None,
        host="localhost",
        port=8902,
        jobs=None,
        workflows=None,
    )
    rc = _cmd_jobs_serve(args)
    assert rc == 0
    assert len(served) == 1


# ── ujin/cli.py: line 434->436 ───────────────────────────────────────────────


def test_explain_comment_lines_empty_prov_returns_empty_list():
    """Branch 434->436: when prov has no 'source' key, _explain_comment_lines
    skips the `lines.append(# source: ...)` block and returns [].

    All existing --explain tests always pass a prov with a "source" key, leaving
    the False branch of `if src:` uncovered.
    """
    import yaml as _yaml

    from ujin.cli import _explain_comment_lines

    # No keys at all → src is None → False branch, returns []
    assert _explain_comment_lines({}, _yaml) == []

    # Only "matrix" with no "source" → still no source line
    result = _explain_comment_lines(
        {"matrix": {"key": "matrix", "index": 0, "vars": {"n": "a"}}},
        _yaml,
    )
    assert not any("source" in line for line in result)
    assert any("matrix" in line for line in result)


# ── ujin/cli.py: lines 601-602 ───────────────────────────────────────────────


def test_open_strategy_feedback_ro_non_database_file_exits(tmp_path):
    """Lines 601-602: _open_strategy_feedback_ro raises SystemExit with a clean
    message when the file exists but is not a valid SQLite database.

    sqlite3 raises DatabaseError when the file header is not a valid db;
    we catch that and re-raise as a SystemExit so callers get a clean message.
    """
    from ujin.cli import _open_strategy_feedback_ro

    bad = tmp_path / "notadb.db"
    bad.write_bytes(b"this is not a sqlite3 database at all\x00\xff\xfe")

    with pytest.raises(SystemExit) as exc:
        _open_strategy_feedback_ro(str(bad))

    msg = str(exc.value)
    assert "not a valid ujin strategy database" in msg
    assert "Traceback" not in msg


# ── ujin/cli.py: line 723 ─────────────────────────────────────────────────────


def test_watch_no_webhook_log_callback_is_called(monkeypatch):
    """Line 723: the `_log(event)` callback defined in the no-webhook `else:`
    branch of _cmd_watch is invoked when an on_change event arrives.

    The callback is a closure, so we capture it by intercepting CallbackSink's
    constructor, then call it with a fake event — covering the log.info body.
    """
    import ujin.cli as _cli
    from ujin.diff.events import CallbackSink

    captured: list = []
    original_init = CallbackSink.__init__

    def _spy_init(self, cb):
        captured.append(cb)
        original_init(self, cb)

    monkeypatch.setattr(CallbackSink, "__init__", _spy_init)

    async def _noop_run(self):
        pass

    monkeypatch.setattr("ujin.engine.PollEngine.run", _noop_run)
    _cli.main(["watch", "http://example.com"])

    # The no-webhook path must have captured exactly one callback.
    assert len(captured) == 1

    class _FakeEvent:
        key = "http://example.com"
        regions = None
        fingerprint = "abc123def456"

    # Calling the callback exercises line 723 (log.info inside _log).
    captured[0](_FakeEvent())
