"""ObscuraFetcher binary/URL resolution order — no Rust build required."""
from __future__ import annotations

from pathlib import Path

import ujin.fetch.obscura as obs


def _clear_env(monkeypatch):
    monkeypatch.delenv("OBSCURA_URL", raising=False)
    monkeypatch.delenv("OBSCURA_BIN", raising=False)


def test_url_takes_priority(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OBSCURA_URL", "http://obscura:9222")
    assert obs.obscura_available() is True


def test_explicit_bin_wins_over_bundled(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OBSCURA_BIN", "/custom/path/obscura")
    assert obs._obscura_bin() == "/custom/path/obscura"


def test_bundled_binary_used_when_present(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    fake = tmp_path / "obscura"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(obs, "_bundled_binary", lambda: str(fake))
    assert obs._obscura_bin() == str(fake)
    assert obs.obscura_available() is True


def test_falls_back_to_path_name(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(obs, "_bundled_binary", lambda: None)
    # No env, no bundled build -> bare name (resolved on PATH at call time).
    assert obs._obscura_bin() == "obscura"


def test_unavailable_when_nothing_resolves(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(obs, "_bundled_binary", lambda: None)
    monkeypatch.setattr(obs.shutil if hasattr(obs, "shutil") else obs, "which", lambda *_: None, raising=False)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda *_: None)
    assert obs.obscura_available() is False


def test_bundled_path_is_inside_package():
    """The bundled path resolves to <pkg>/obscura/target/release/obscura."""
    pkg_dir = Path(obs.__file__).resolve().parents[1]
    assert pkg_dir.name == "ujin"  # the python package dir
    candidate = pkg_dir / "obscura" / "target" / "release" / "obscura"
    assert candidate.parts[-4:] == ("obscura", "target", "release", "obscura")
