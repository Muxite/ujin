"""Opt-in browser integration test — drives a REAL headless Chromium against a
local HTML fixture with a working "load more" button, asserting full exhaustion.

NOT part of the default CI lane. Run it explicitly:

    UJIN_BROWSER_IT=1 pytest tests/integration/test_browser_real.py

Requires `pip install 'ujin[browser]'` + `playwright install chromium`.
"""
from __future__ import annotations

import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

from ujin.fetch.browser import browser_available

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = [
    pytest.mark.skipif(not os.environ.get("UJIN_BROWSER_IT"),
                       reason="set UJIN_BROWSER_IT=1 to run the real-browser test"),
    pytest.mark.skipif(not browser_available("playwright"),
                       reason="playwright not installed"),
]


@pytest.fixture
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(_FIXTURES))
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


async def test_load_more_exhausts_against_real_chromium(server):
    from ujin.fetch.browser import BrowserFetcher

    fetcher = BrowserFetcher(engine="playwright", headless=True)
    try:
        result = await fetcher.render(
            f"{server}/load_more.html",
            [{"action": "load_more", "button": "button.load-more",
              "results": ".publication-item", "max_clicks": 50,
              "timeout_ms": 30000, "settle_ms": 300}],
            results_selector=".publication-item",
        )
    finally:
        await fetcher.close()

    # the fixture exposes 37 items across 4 clicks, then removes the button
    assert result.items is not None
    assert len(result.items) == 37
    log = [a for a in result.actions_log if a["action"] == "load_more"]
    assert log and log[0]["final_count"] == 37
    assert log[0]["stopped"] in ("button_gone", "stable")
