"""MCP server: tools exercised through an in-memory MCP client session,
backed by fakes (no network, no real jobs db outside tmp)."""
from __future__ import annotations

import json

import pytest

mcp_sdk = pytest.importorskip("mcp")

from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session as client_session,
)

from conftest import FakeHttp, FakeObscura  # noqa: E402
from ujin.cache import HostPolicy, ScrapeCache  # noqa: E402
from ujin.engine import PollEngine  # noqa: E402
from ujin.fetch.http import HttpResponse  # noqa: E402
from ujin.jobs.manager import JobManager  # noqa: E402
from ujin.jobs.store import JobStore  # noqa: E402
from ujin.mcp.server import _Backend, _json_safe, create_mcp_server  # noqa: E402
from ujin.scrape.config import ScrapeConfig  # noqa: E402
from ujin.scrape.service import ScrapeService  # noqa: E402

HOME = "https://news.example.com/"


def _index_html(n=8):
    rows = "".join(
        f'<article><h2><a href="{HOME}2026/06/09/story-{i}">'
        f"A sufficiently long headline number {i}</a></h2></article>"
        for i in range(n)
    )
    return f"<html><body><main>{rows}</main></body></html>"


@pytest.fixture
def backend(tmp_path):
    b = _Backend()
    b.scrape_service = ScrapeService(
        http=FakeHttp({HOME: HttpResponse(url=HOME, status=200,
                                          body=_index_html(), final_url=HOME)}),
        obscura=FakeObscura(),
        cache=ScrapeCache(),
        policy=HostPolicy(cooldown_secs=60),
        config=ScrapeConfig(),
    )
    b.manager = JobManager(PollEngine(), JobStore(tmp_path / "jobs.db"),
                           scrape_service=b.scrape_service)
    return b


def _payload(result):
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


def _payload_list(result):
    # FastMCP renders a list return as one TextContent per element
    assert not result.isError, result.content
    return [json.loads(c.text) for c in result.content]


async def test_tool_listing(backend):
    server = create_mcp_server(backend)
    async with client_session(server._mcp_server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {
        "scrape_url", "scrape_feed", "discover_site", "get_capabilities",
        "get_metrics", "list_jobs", "get_job", "create_job", "run_job",
        "pause_job", "resume_job", "get_job_results",
    }


async def test_scrape_url_tool(backend):
    server = create_mcp_server(backend)
    async with client_session(server._mcp_server) as client:
        result = await client.call_tool("scrape_url", {"url": HOME})
    body = _payload(result)
    assert body["kind"] == "links"
    assert len(body["links"]) == 8
    assert body["strategy_used"] == "http"
    # hct-site-compatible field names survive the MCP path too
    for f in ("url", "fingerprint", "used_renderer", "strategy_used"):
        assert f in body


async def test_get_capabilities_tool(backend):
    server = create_mcp_server(backend)
    async with client_session(server._mcp_server) as client:
        body = _payload(await client.call_tool("get_capabilities", {}))
    assert set(body["backends"]) == {"http", "obscura", "playwright", "selenium"}
    assert body["backends"]["http"]["available"] is True


async def test_job_lifecycle_via_mcp(backend):
    server = create_mcp_server(backend)
    async with client_session(server._mcp_server) as client:
        created = _payload(await client.call_tool("create_job", {"spec": {
            "name": "echo-job",
            "source": {"kind": "command", "config": {"argv": ["echo", "hi"]}},
            "schedule": {"mode": "adaptive", "base": 60},
        }}))
        job_id = created["id"]

        jobs = _payload_list(await client.call_tool("list_jobs", {}))
        assert any(j["id"] == job_id for j in jobs)

        ran = _payload(await client.call_tool("run_job", {"job_id": job_id}))
        assert ran["ok"] is True and ran["changed"] is True

        results = _payload(await client.call_tool(
            "get_job_results", {"job_id": job_id}))
        assert len(results["results"]) == 1

        assert _payload(await client.call_tool(
            "pause_job", {"job_id": job_id}))["paused"] is True
        got = _payload(await client.call_tool("get_job", {"job_id": job_id}))
        assert got["state"] == "paused"
        assert _payload(await client.call_tool(
            "resume_job", {"job_id": job_id}))["resumed"] is True


async def test_unknown_job_errors_are_soft(backend):
    server = create_mcp_server(backend)
    async with client_session(server._mcp_server) as client:
        body = _payload(await client.call_tool("run_job", {"job_id": "ghost"}))
        assert "error" in body
        body = _payload(await client.call_tool("get_job", {"job_id": "ghost"}))
        assert "error" in body
        body = _payload(await client.call_tool("get_job_results",
                                               {"job_id": "ghost"}))
        assert "error" in body


async def test_get_metrics_tool(backend):
    server = create_mcp_server(backend)
    async with client_session(server._mcp_server) as client:
        await client.call_tool("scrape_url", {"url": HOME})
        body = _payload(await client.call_tool("get_metrics", {}))
    assert body["total_fetches"] >= 1


def test_json_safe_handles_dataclasses_and_bytes():
    import dataclasses

    @dataclasses.dataclass
    class Inner:
        blob: bytes

    @dataclasses.dataclass
    class Outer:
        items: list
        inner: Inner

    out = _json_safe(Outer(items=[1, (2, 3)], inner=Inner(blob=b"abcd")))
    json.dumps(out)
    assert out["inner"]["blob"] == "<4 bytes>"
    assert out["items"] == [1, [2, 3]]


def test_cli_mcp_serve_dispatch(monkeypatch):
    import ujin.cli as cli

    called = {}

    def fake_serve(transport, host, port):
        called.update(transport=transport, host=host, port=port)

    monkeypatch.setattr("ujin.mcp.server.serve", fake_serve)
    rc = cli.main(["mcp-serve", "--http", "--port", "9999"])
    assert rc == 0
    assert called == {"transport": "http", "host": "127.0.0.1", "port": 9999}
