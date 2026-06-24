"""Tests for ``ujin plan render --summary`` (offline, deterministic).

``--summary`` prints a concise overview — total job count then one line per
job (id, source kind/url, schedule, number of sinks) — instead of the full
expanded YAML/JSON specs.  The default render output must be byte-for-byte
unchanged when the flag is absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ujin.cli import _render_summary, main
from ujin.jobs.app import _load_ingest_plan


# ── helpers ──────────────────────────────────────────────────────────────────── #

def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _simple_plan(jobs: list) -> dict:
    return {"jobs": jobs}


def _http_job(job_id: str, url: str, n_sinks: int = 0) -> dict:
    job: dict = {
        "id": job_id,
        "source": {"kind": "http", "config": {"url": url}},
    }
    if n_sinks:
        job["sinks"] = [{"kind": "webhook", "config": {"url": "https://sink/"}}
                        for _ in range(n_sinks)]
    return job


# ── _render_summary unit tests ────────────────────────────────────────────────── #

def test_render_summary_job_count_plural(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([
        _http_job("alpha", "https://a/"),
        _http_job("beta", "https://b/"),
    ]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert out.startswith("2 jobs\n")


def test_render_summary_job_count_singular(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([_http_job("solo", "https://s/")]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert out.startswith("1 job\n")


def test_render_summary_includes_id_and_source_url(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([_http_job("feed-a", "https://feed.example.com/")]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert "feed-a" in out
    assert "http:https://feed.example.com/" in out


def test_render_summary_source_without_url(tmp_path):
    """A source with no url key in config omits the colon."""
    _write(tmp_path / "p.yaml", _simple_plan([{
        "id": "cmd-job",
        "source": {"kind": "command", "config": {"cmd": ["echo", "hi"]}},
    }]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    line = [l for l in out.splitlines() if "cmd-job" in l][0]
    assert "command" in line
    assert "command:" not in line


def test_render_summary_adaptive_schedule(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([{
        "id": "j",
        "source": {"kind": "http", "config": {"url": "https://x/"}},
        "schedule": {"mode": "adaptive", "base": 120},
    }]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert "adaptive/120s" in out


def test_render_summary_poll_schedule(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([{
        "id": "j",
        "source": {"kind": "http", "config": {"url": "https://x/"}},
        "schedule": {"mode": "poll", "base": 30},
    }]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert "poll/30s" in out


def test_render_summary_cron_schedule(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([{
        "id": "cron-j",
        "source": {"kind": "http", "config": {"url": "https://x/"}},
        "schedule": {"mode": "cron", "cron": "*/15 * * * *"},
    }]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert "cron/*/15 * * * *" in out


def test_render_summary_once_schedule(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([{
        "id": "one-shot",
        "source": {"kind": "http", "config": {"url": "https://x/"}},
        "schedule": {"mode": "once"},
    }]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert "once" in out
    # must not emit "once/..." (no base suffix for once mode)
    assert "once/" not in out


def test_render_summary_sinks_plural(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([_http_job("j", "https://x/", n_sinks=3)]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert "3 sinks" in out


def test_render_summary_sink_singular(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([_http_job("j", "https://x/", n_sinks=1)]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert "1 sink" in out
    assert "1 sinks" not in out


def test_render_summary_zero_sinks(tmp_path):
    _write(tmp_path / "p.yaml", _simple_plan([_http_job("j", "https://x/", n_sinks=0)]))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    assert "0 sinks" in out


def test_render_summary_one_line_per_job(tmp_path):
    jobs = [_http_job(f"job-{i}", f"https://x{i}/") for i in range(4)]
    _write(tmp_path / "p.yaml", _simple_plan(jobs))
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    out = _render_summary(specs)
    lines = out.splitlines()
    # header + one line per job
    assert len(lines) == 5


# ── CLI: plan render --summary via main() ─────────────────────────────────────── #

def test_cli_summary_exits_zero(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _simple_plan([_http_job("a", "https://a/"), _http_job("b", "https://b/")]))
    rc = main(["plan", "render", str(p), "--summary"])
    assert rc == 0


def test_cli_summary_output_job_count(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _simple_plan([_http_job("a", "https://a/"), _http_job("b", "https://b/")]))
    main(["plan", "render", str(p), "--summary"])
    out = capsys.readouterr().out
    assert out.startswith("2 jobs\n")


def test_cli_summary_output_contains_ids(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _simple_plan([_http_job("feed-x", "https://x/"), _http_job("feed-y", "https://y/")]))
    main(["plan", "render", str(p), "--summary"])
    out = capsys.readouterr().out
    assert "feed-x" in out and "feed-y" in out


def test_cli_summary_does_not_emit_yaml_docs(tmp_path, capsys):
    """--summary must not output the YAML --- document separators."""
    p = tmp_path / "p.yaml"
    _write(p, _simple_plan([_http_job("a", "https://a/")]))
    main(["plan", "render", str(p), "--summary"])
    out = capsys.readouterr().out
    assert "---" not in out


def test_cli_no_summary_flag_is_unchanged(tmp_path, capsys):
    """Default render (no --summary) must not contain the summary header."""
    p = tmp_path / "p.yaml"
    _write(p, _simple_plan([_http_job("a", "https://a/")]))
    rc = main(["plan", "render", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    # Default output starts with YAML document separator, not "N jobs"
    assert out.startswith("---")
    assert "jobs" not in out.splitlines()[0]


def test_cli_summary_with_only_filter(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _simple_plan([_http_job("keep", "https://k/"), _http_job("drop", "https://d/")]))
    main(["plan", "render", str(p), "--summary", "--only", "keep"])
    out = capsys.readouterr().out
    assert out.startswith("1 job\n")
    assert "keep" in out
    assert "drop" not in out


def test_cli_summary_with_exclude_filter(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _simple_plan([_http_job("a", "https://a/"), _http_job("excluded-job", "https://b/")]))
    main(["plan", "render", str(p), "--summary", "--exclude", "excluded-job"])
    out = capsys.readouterr().out
    assert out.startswith("1 job\n")
    assert "excluded-job" not in out


def test_cli_summary_missing_path_exits_nonzero(tmp_path, capsys):
    rc = main(["plan", "render", str(tmp_path / "nonexistent.yaml"), "--summary"])
    assert rc == 1


# ── summary for a workflows directory ────────────────────────────────────────── #

def test_cli_summary_workflows_dir(tmp_path, capsys):
    wdir = tmp_path / "workflows"
    wdir.mkdir()
    _write(wdir / "alpha.yaml", {"id": "alpha", "source": {"kind": "http",
                                                             "config": {"url": "https://a/"}}})
    _write(wdir / "beta.yaml", {"id": "beta", "source": {"kind": "rss",
                                                           "config": {"url": "https://b/feed"}}})
    rc = main(["plan", "render", str(wdir), "--summary"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("2 jobs\n")
    assert "alpha" in out and "beta" in out
    assert "http:https://a/" in out
    assert "rss:https://b/feed" in out


def test_cli_summary_workflows_dir_single_file(tmp_path, capsys):
    wdir = tmp_path / "workflows"
    wdir.mkdir()
    _write(wdir / "only.yaml", {"id": "only-job", "source": {"kind": "http",
                                                               "config": {"url": "https://x/"}}})
    main(["plan", "render", str(wdir), "--summary"])
    out = capsys.readouterr().out
    assert out.startswith("1 job\n")
    assert "only-job" in out


# ── matrix-expanded plan ──────────────────────────────────────────────────────── #

def test_cli_summary_matrix_expanded(tmp_path, capsys):
    """--summary works after matrix fan-out; one line per expanded job."""
    p = tmp_path / "p.yaml"
    _write(p, {
        "jobs": [{
            "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
            "matrix": {"slug": ["a", "b", "c"]},
        }]
    })
    rc = main(["plan", "render", str(p), "--summary"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("3 jobs\n")
    lines = out.splitlines()
    assert len(lines) == 4  # header + 3 expanded jobs
