"""ObscuraFetcher error paths — binary mode via a stub script, HTTP mode via
the fake origin. The real Rust binary is never invoked.
"""
from __future__ import annotations

import pytest

from conftest import make_obscura_stub
from ujin.fetch.obscura import (
    ObscuraError,
    ObscuraFetcher,
    ObscuraTimeout,
    obscura_available,
)



# ── binary mode ──────────────────────────────────────────────────────────────

async def test_binary_renders_html(obscura_stub_bin):
    result = await ObscuraFetcher(timeout_secs=10).render_html("https://x.test/p")
    assert "rendered: https://x.test/p" in result.html
    assert result.url == "https://x.test/p"
    assert result.elapsed_ms >= 0


async def test_binary_nonzero_exit_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSCURA_BIN", make_obscura_stub(tmp_path, "fail"))
    monkeypatch.delenv("OBSCURA_URL", raising=False)
    with pytest.raises(ObscuraError, match="exited 3"):
        await ObscuraFetcher(timeout_secs=10).render_html("https://x.test/")


async def test_binary_missing_raises_helpful_error(monkeypatch):
    monkeypatch.setenv("OBSCURA_BIN", "/nonexistent/obscura")
    monkeypatch.delenv("OBSCURA_URL", raising=False)
    with pytest.raises(ObscuraError, match="binary not found"):
        await ObscuraFetcher().render_html("https://x.test/")


async def test_binary_hang_times_out(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSCURA_BIN", make_obscura_stub(tmp_path, "hang"))
    monkeypatch.delenv("OBSCURA_URL", raising=False)
    # communicate() waits timeout+5 — drive it (just) below a second.
    with pytest.raises(ObscuraTimeout):
        await ObscuraFetcher(timeout_secs=-4.7).render_html("https://x.test/")


# ── HTTP service mode ────────────────────────────────────────────────────────

async def test_http_mode_renders(fake_origin, monkeypatch):
    fake_origin.add("/render", body='{"html": "<html>via-service</html>"}',
                    content_type="application/json")
    monkeypatch.setenv("OBSCURA_URL", fake_origin.url("/").rstrip("/"))
    result = await ObscuraFetcher(timeout_secs=5).render_html("https://x.test/")
    assert result.html == "<html>via-service</html>"
    # service got the url we asked for
    assert fake_origin.requests[-1].method == "POST"


async def test_http_mode_missing_html_key_yields_empty(fake_origin, monkeypatch):
    fake_origin.add("/render", body='{"other": 1}',
                    content_type="application/json")
    monkeypatch.setenv("OBSCURA_URL", fake_origin.url("/").rstrip("/"))
    result = await ObscuraFetcher(timeout_secs=5).render_html("https://x.test/")
    assert result.html == ""


async def test_http_mode_non_200_raises(fake_origin, monkeypatch):
    fake_origin.add("/render", body="busy", status=503)
    monkeypatch.setenv("OBSCURA_URL", fake_origin.url("/").rstrip("/"))
    with pytest.raises(ObscuraError, match="HTTP 503"):
        await ObscuraFetcher(timeout_secs=5).render_html("https://x.test/")


async def test_http_mode_connection_refused_raises(monkeypatch):
    monkeypatch.setenv("OBSCURA_URL", "http://127.0.0.1:1")
    with pytest.raises(ObscuraError, match="service error"):
        await ObscuraFetcher(timeout_secs=1).render_html("https://x.test/")


async def test_http_mode_preferred_over_binary(fake_origin, monkeypatch, tmp_path):
    """When both OBSCURA_URL and OBSCURA_BIN are set, HTTP wins."""
    fake_origin.add("/render", body='{"html": "service"}',
                    content_type="application/json")
    monkeypatch.setenv("OBSCURA_BIN", make_obscura_stub(tmp_path, "ok"))
    monkeypatch.setenv("OBSCURA_URL", fake_origin.url("/").rstrip("/"))
    result = await ObscuraFetcher(timeout_secs=5).render_html("https://x.test/")
    assert result.html == "service"


# ── availability ─────────────────────────────────────────────────────────────

def test_available_with_url(monkeypatch):
    monkeypatch.setenv("OBSCURA_URL", "http://localhost:9222")
    assert obscura_available() is True


def test_available_with_stub_bin(obscura_stub_bin):
    assert obscura_available() is True


def test_unavailable_with_bogus_bin(monkeypatch):
    monkeypatch.setenv("OBSCURA_BIN", "/nonexistent/obscura")
    monkeypatch.delenv("OBSCURA_URL", raising=False)
    assert obscura_available() is False
