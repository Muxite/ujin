"""ujin MCP server — expose scrape + jobs as tools for agents.

``ujin mcp-serve`` runs a Model Context Protocol server over stdio (default)
or streamable HTTP, backed by the same ScrapeService / JobManager wiring the
:8901 and :8902 services use — direct Python calls, no HTTP hop.

Needs the ``mcp`` extra: ``pip install 'ujin[mcp]'``. See docs/MCP.md.
"""
from __future__ import annotations

__all__ = ["create_mcp_server", "serve"]


def __getattr__(name: str):
    if name in __all__:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
