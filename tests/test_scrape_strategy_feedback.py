"""ScrapeService ↔ StrategyFeedback closed-loop tests.

Exercises the opt-in ``learn_strategy`` flag: outcomes are recorded per host,
a prior success biases which backend the ``auto`` path tries first, a penalized
recommendation is ignored, and — crucially — with the flag off the service is
byte-identical (nothing is recorded, no bias is applied).

All offline/deterministic: duck-typed FakeHttp/FakeObscura, in-memory
StrategyFeedback, hand-built HostRecord/SiteStore for the penalty path.
"""
from __future__ import annotations

import pytest

from ujin.adapt import StrategyFeedback
from ujin.adapt.site_store import HostRecord
from ujin.cache import HostPolicy, ScrapeCache
from ujin.fetch.http import HttpResponse
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.service import ScrapeService

from conftest import FakeHttp, FakeObscura

pytestmark = pytest.mark.asyncio

HTTP = ("http", "html")
OBSCURA = ("obscura", "js")


def _links_html(base: str, n: int = 8) -> str:
    """HTML with ``n`` distinct headline links, each over the 30-char gate."""
    anchors = "".join(
        f'<a href="{base}story-{i}">A sufficiently long headline number {i} here</a>'
        for i in range(n)
    )
    return f"<html><body><main>{anchors}</main></body></html>"


def _service(
    http,
    *,
    obscura=None,
    cache=None,
    policy=None,
    strategy=None,
    site_store=None,
    learn=False,
    browser=None,
):
    cfg = ScrapeConfig(learn_strategy=learn)
    return ScrapeService(
        http=http,
        obscura=obscura or FakeObscura(),
        cache=cache or ScrapeCache(),
        policy=policy or HostPolicy(cooldown_secs=60),
        config=cfg,
        strategy_feedback=strategy,
        site_store=site_store,
        browser=browser,
    )


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #

async def test_records_outcome_when_enabled():
    home = "https://news.example.com/"
    routes = {home: HttpResponse(url=home, status=200, body=_links_html(home),
                                 final_url=home)}
    fb = StrategyFeedback(":memory:")
    svc = _service(FakeHttp(routes), strategy=fb, learn=True)

    result = await svc.scrape(home, mode="links")

    assert result.strategy_used == "http"
    # A successful HTTP fetch was recorded for the host.
    assert fb.recommend("news.example.com") == HTTP
    fb.close()


async def test_failure_then_success_recorded_per_backend():
    """HTTP 403 → obscura escalation records http=fail, obscura=ok."""
    home = "https://spa.example.com/"
    routes = {home: HttpResponse(url=home, status=403, body="", final_url=home)}
    obscura = FakeObscura(html=_links_html(home))
    fb = StrategyFeedback(":memory:")
    svc = _service(FakeHttp(routes), obscura=obscura, strategy=fb, learn=True)

    result = await svc.scrape(home, mode="links")

    assert result.strategy_used == "obscura"
    # obscura succeeded (1/1) and http failed (0/1) → obscura is the winner.
    assert fb.recommend("spa.example.com") == OBSCURA
    fb.close()


async def test_flag_off_records_nothing():
    """A feedback store is wired but the flag is off → no recording, no bias."""
    home = "https://news.example.com/"
    routes = {home: HttpResponse(url=home, status=200, body=_links_html(home),
                                 final_url=home)}
    fb = StrategyFeedback(":memory:")
    svc = _service(FakeHttp(routes), strategy=fb, learn=False)

    await svc.scrape(home, mode="links")

    assert fb.recommend("news.example.com") is None
    fb.close()


# --------------------------------------------------------------------------- #
# Bias: recommend() changes which backend is tried first
# --------------------------------------------------------------------------- #

async def test_recommend_biases_first_backend_tried():
    """A prior obscura win makes the auto path try obscura before http."""
    home = "https://spa.example.com/"
    fb = StrategyFeedback(":memory:")
    fb.record("spa.example.com", OBSCURA, ok=True, latency=0.1)

    http = FakeHttp({home: HttpResponse(url=home, status=200,
                                        body=_links_html(home), final_url=home)})
    obscura = FakeObscura(html=_links_html(home))
    svc = _service(http, obscura=obscura, strategy=fb, learn=True)

    result = await svc.scrape(home, mode="links")

    # obscura was tried; HTTP was skipped entirely — the recommendation won.
    assert result.strategy_used == "obscura"
    assert obscura.calls == [home]
    assert http.calls == []
    fb.close()


async def test_no_bias_when_flag_off_tries_http_first():
    """Same prior obscura win, but flag off → unchanged auto order (http first)."""
    home = "https://spa.example.com/"
    fb = StrategyFeedback(":memory:")
    fb.record("spa.example.com", OBSCURA, ok=True, latency=0.1)

    http = FakeHttp({home: HttpResponse(url=home, status=200,
                                        body=_links_html(home), final_url=home)})
    obscura = FakeObscura(html=_links_html(home))
    svc = _service(http, obscura=obscura, strategy=fb, learn=False)

    result = await svc.scrape(home, mode="links")

    assert result.strategy_used == "http"
    assert http.calls == [home]
    assert obscura.calls == []
    fb.close()


async def test_unseen_host_falls_back_to_auto_order():
    """No recommendation for the host → http-first auto order is preserved."""
    home = "https://fresh.example.com/"
    fb = StrategyFeedback(":memory:")  # nothing recorded for this host
    http = FakeHttp({home: HttpResponse(url=home, status=200,
                                        body=_links_html(home), final_url=home)})
    obscura = FakeObscura(html=_links_html(home))
    svc = _service(http, obscura=obscura, strategy=fb, learn=True)

    result = await svc.scrape(home, mode="links")

    assert result.strategy_used == "http"
    assert http.calls == [home]
    assert obscura.calls == []
    fb.close()


# --------------------------------------------------------------------------- #
# Penalization: a flagged recommendation is avoided
# --------------------------------------------------------------------------- #

class _FixedStore:
    """Minimal SiteStore stand-in: returns a fixed HostRecord per host."""

    def __init__(self, record: HostRecord):
        self._record = record

    def get(self, host: str) -> HostRecord:
        return self._record


async def test_penalized_recommendation_is_avoided():
    """When the host is rate-limited, the obscura recommendation is skipped and
    the auto order (http first) is used instead."""
    home = "https://spa.example.com/"
    fb = StrategyFeedback(":memory:")
    fb.record("spa.example.com", OBSCURA, ok=True, latency=0.1)

    # A rate-limited record → is_penalized() True for every strategy.
    store = _FixedStore(HostRecord(host="spa.example.com", rate_limit_count=3))
    http = FakeHttp({home: HttpResponse(url=home, status=200,
                                        body=_links_html(home), final_url=home)})
    obscura = FakeObscura(html=_links_html(home))
    svc = _service(http, obscura=obscura, strategy=fb, site_store=store, learn=True)

    result = await svc.scrape(home, mode="links")

    # Bias was suppressed: http tried first despite the obscura recommendation.
    assert result.strategy_used == "http"
    assert http.calls == [home]
    assert obscura.calls == []
    fb.close()


async def test_clean_record_does_not_suppress_bias():
    """A healthy SiteStore record leaves the recommendation in force."""
    home = "https://spa.example.com/"
    fb = StrategyFeedback(":memory:")
    fb.record("spa.example.com", OBSCURA, ok=True, latency=0.1)

    store = _FixedStore(HostRecord(host="spa.example.com"))  # clean → health 1.0
    http = FakeHttp({home: HttpResponse(url=home, status=200,
                                        body=_links_html(home), final_url=home)})
    obscura = FakeObscura(html=_links_html(home))
    svc = _service(http, obscura=obscura, strategy=fb, site_store=store, learn=True)

    result = await svc.scrape(home, mode="links")

    assert result.strategy_used == "obscura"
    assert obscura.calls == [home]
    fb.close()


# --------------------------------------------------------------------------- #
# Closed loop: an observed failure flips the next selection
# --------------------------------------------------------------------------- #

async def test_loop_closes_failure_flips_next_choice():
    """First scrape sees http fail → obscura win; second scrape biases obscura."""
    home = "https://spa.example.com/"
    fb = StrategyFeedback(":memory:")
    obscura = FakeObscura(html=_links_html(home))

    # Round 1: http 403 forces the obscura escalation; outcomes recorded.
    http1 = FakeHttp({home: HttpResponse(url=home, status=403, body="", final_url=home)})
    svc1 = _service(http1, obscura=obscura, strategy=fb, learn=True)
    r1 = await svc1.scrape(home, mode="links")
    assert r1.strategy_used == "obscura"

    # Round 2: a fresh service over the same store now biases obscura-first, so
    # http is never even attempted.
    http2 = FakeHttp({home: HttpResponse(url=home, status=200,
                                         body=_links_html(home), final_url=home)})
    svc2 = _service(http2, obscura=obscura, strategy=fb, learn=True)
    r2 = await svc2.scrape(home, mode="links", force_refresh=True)
    assert r2.strategy_used == "obscura"
    assert http2.calls == []
    fb.close()
