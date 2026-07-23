"""Tests for the scrape app's scorer resolution:

  1. ``ujin.trends.wiring.wire_breaking_scorer`` — the extracted unit that
     used to live inline in ``create_scrape_app`` (news-trading semantics).
  2. A regression net proving that extraction didn't change
     ``create_scrape_app(ScrapeConfig(enable_breaking_scorer=True))``'s
     observable behavior.
  3. The new ``ScrapeConfig.scorer_kind`` foreign-code path: a plugin dropped
     into ``UJIN_PLUGINS_DIR`` supplying a ``@register.scorer`` kind, resolved
     with zero ujin-tree edits — plus the unknown-kind fallback and the
     ``POST /plugins/reload`` endpoint added for symmetry with the jobs app.

All offline — no live network; the x-trends refresh loop's one call is
monkeypatched to fail fast instead of reaching out.
"""
from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.registry import register as _reg  # noqa: E402
from ujin.scrape.app import create_scrape_app  # noqa: E402
from ujin.scrape.config import ScrapeConfig  # noqa: E402
from ujin.scrape.scoring import NullScorer  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    # the global registry persists across tests in one process; drop plugin
    # entries before and after each test so they don't leak (see test_registry.py)
    _reg.clear_plugins()
    yield
    _reg.clear_plugins()


async def _fail_fast_trends(region, limit):
    raise RuntimeError("no network in tests")


# ── ujin.trends.wiring.wire_breaking_scorer ─────────────────────────────────

async def test_wire_breaking_scorer_builds_working_scorer_from_config(monkeypatch):
    """wire_breaking_scorer(cfg) must produce a real BreakingScorer wired to a
    CorroborationStore whose thresholds/window are exactly what cfg says."""
    import ujin.sources.social as _social
    from ujin.trends import BreakingScorer, CorroborationStore
    from ujin.trends.wiring import BreakingScorerWiring, wire_breaking_scorer

    monkeypatch.setattr(_social, "fetch_x_trends", _fail_fast_trends)

    cfg = ScrapeConfig(
        corroboration_min_hosts=2,
        corroboration_max_hosts_for_full_score=2,
        headline_ring_max=64,
        breaking_threshold=0.4,
    )
    wiring = wire_breaking_scorer(cfg)
    try:
        assert isinstance(wiring, BreakingScorerWiring)
        assert isinstance(wiring.scorer, BreakingScorer)
        assert isinstance(wiring.corroboration, CorroborationStore)
        # headline_ring_max threaded into the store's ring buffer size.
        assert wiring.corroboration._entries.maxlen == 64

        # min_hosts=2 == max_hosts_for_full_score=2 -> two corroborating
        # hosts hits the full-score ceiling exactly (proves both thresholds
        # were plumbed from cfg, not left at CorroborationStore's defaults of
        # 3/5, which would need a third host to score at all).
        headline = "Senate passes major spending bill in late-night vote"
        wiring.corroboration.add(headline, "apnews.com")
        score, n = wiring.corroboration.lookup_score(headline, "cnn.com")
        assert n == 2
        assert score == 1.0

        assert isinstance(wiring.refresh_task, asyncio.Task)
        assert not wiring.refresh_task.done()
    finally:
        await wiring.aclose()

    # aclose() cancels cleanly: no exception, no lingering task.
    assert wiring.refresh_task.done()
    assert wiring.refresh_task.cancelled()


async def test_wire_breaking_scorer_aclose_is_idempotent_safe(monkeypatch):
    """Calling aclose() awaits the already-cancelled task without raising."""
    import ujin.sources.social as _social
    from ujin.trends.wiring import wire_breaking_scorer

    monkeypatch.setattr(_social, "fetch_x_trends", _fail_fast_trends)
    wiring = wire_breaking_scorer(ScrapeConfig())
    await wiring.aclose()
    # a second aclose on an already-cancelled/done task must not blow up
    await wiring.aclose()
    assert wiring.refresh_task.cancelled()


# ── regression: enable_breaking_scorer=True app wiring unchanged ────────────

def test_create_scrape_app_breaking_scorer_state_unchanged(monkeypatch):
    """The wire_breaking_scorer extraction must be a pure refactor: the app's
    observable state (scorer type, app.state.corroboration, app.state.
    nitter_pool, /health) and its clean teardown must be identical to before."""
    import ujin.sources.social as _social
    from ujin.trends import BreakingScorer, CorroborationStore

    monkeypatch.setattr(_social, "fetch_x_trends", _fail_fast_trends)
    cfg = ScrapeConfig(enable_breaking_scorer=True)
    app = create_scrape_app(cfg)
    with TestClient(app) as c:
        assert isinstance(app.state.service._scorer, BreakingScorer)
        assert isinstance(app.state.corroboration, CorroborationStore)
        assert app.state.nitter_pool is None  # no nitter_pool_path configured
        assert app.state.scorer_error is None
        body = c.get("/health").json()
        assert body["status"] == "ok"
        assert body["scorer_error"] is None
    # TestClient.__exit__ ran the lifespan teardown (wiring.aclose() +
    # close_scrape_components) with no exception escaping — that's the load-
    # bearing assertion: pytest would fail loudly on an unhandled exception
    # raised during shutdown.


def test_create_scrape_app_breaking_scorer_plus_nitter_pool_both_wire(monkeypatch):
    """nitter_pool_path + enable_breaking_scorer together: nitter_pool must
    keep wiring independently of the scorer resolution branch (it is not
    trends-specific — routes_social.py's /social endpoints use it regardless
    of whether a BreakingScorer is active)."""
    import ujin.sources.social as _social

    class _FakePool:
        mirrors = [object()]

    class _FakeNitterPool:
        @classmethod
        def from_yaml(cls, path):
            return _FakePool()

    monkeypatch.setattr(_social, "fetch_x_trends", _fail_fast_trends)
    monkeypatch.setattr(_social, "NitterPool", _FakeNitterPool)
    cfg = ScrapeConfig(enable_breaking_scorer=True, nitter_pool_path="/fake/pool.yaml")
    app = create_scrape_app(cfg)
    with TestClient(app):
        assert isinstance(app.state.nitter_pool, _FakePool)
        assert app.state.corroboration is not None


# ── ScrapeConfig.scorer_kind: foreign/plugin-code scorer resolution ─────────

_SCORER_PLUGIN = '''
from ujin import register


class _MyTestScorer:
    def score_links(self, links, *, base_url):
        pass

    def poll_hint(self, links, *, base_url, prior_unchanged):
        return None


@register.scorer("my_test_scorer")
def make(cfg):
    return _MyTestScorer()
'''


def test_scorer_kind_resolves_plugin_scorer_end_to_end(tmp_path, monkeypatch):
    """A scorer dropped into UJIN_PLUGINS_DIR and named via scorer_kind must
    become the app's real scorer — the concrete proof the foreign-code path
    works, not just NullScorer silently swallowing everything."""
    plugdir = tmp_path / "plugins"
    plugdir.mkdir()
    (plugdir / "my_scorer.py").write_text(_SCORER_PLUGIN)
    monkeypatch.setenv("UJIN_PLUGINS_DIR", str(plugdir))

    cfg = ScrapeConfig(scorer_kind="my_test_scorer")
    app = create_scrape_app(cfg)
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert "my_scorer" in health["plugins"]["loaded"]
        assert health["plugins"]["failed"] == []
        assert health["scorer_error"] is None

        scorer = app.state.service._scorer
        assert type(scorer).__name__ == "_MyTestScorer"
        assert not isinstance(scorer, NullScorer)


def test_scorer_kind_unknown_falls_back_to_null_scorer_and_reports_on_health():
    """A mistyped/unregistered scorer_kind is a config error, not a reason to
    refuse to serve: the app must still start with NullScorer and surface the
    problem on /health rather than crashing at boot."""
    cfg = ScrapeConfig(scorer_kind="does_not_exist")
    app = create_scrape_app(cfg)
    with TestClient(app) as c:
        assert isinstance(app.state.service._scorer, NullScorer)
        body = c.get("/health").json()
        assert body["status"] == "ok"  # app still serves despite the bad config
        assert body["scorer_error"] is not None
        assert "does_not_exist" in body["scorer_error"]


def test_scorer_kind_ignored_when_explicit_scorer_or_breaking_scorer_set(monkeypatch):
    """scorer= and enable_breaking_scorer both take precedence over scorer_kind
    (matches the documented resolution order)."""
    import ujin.sources.social as _social

    monkeypatch.setattr(_social, "fetch_x_trends", _fail_fast_trends)

    explicit = NullScorer()
    cfg = ScrapeConfig(scorer_kind="does_not_exist")
    app = create_scrape_app(cfg, scorer=explicit)
    with TestClient(app):
        assert app.state.service._scorer is explicit
        assert app.state.scorer_error is None  # scorer_kind branch never ran

    from ujin.trends import BreakingScorer

    cfg2 = ScrapeConfig(enable_breaking_scorer=True, scorer_kind="does_not_exist")
    app2 = create_scrape_app(cfg2)
    with TestClient(app2):
        assert isinstance(app2.state.service._scorer, BreakingScorer)
        assert app2.state.scorer_error is None  # scorer_kind branch never ran


# ── POST /plugins/reload (symmetry with ujin.jobs.app) ──────────────────────

def test_scrape_app_plugins_reload_endpoint(tmp_path, monkeypatch):
    plugdir = tmp_path / "plugins"
    plugdir.mkdir()
    monkeypatch.setenv("UJIN_PLUGINS_DIR", str(plugdir))

    app = create_scrape_app(ScrapeConfig())
    with TestClient(app) as c:
        assert c.get("/health").json()["plugins"] == {"loaded": [], "failed": []}

        (plugdir / "my_scorer.py").write_text(_SCORER_PLUGIN)
        resp = c.post("/plugins/reload")
        assert resp.status_code == 200
        assert resp.json() == {"loaded": ["my_scorer"], "failed": []}
        assert _reg.has("scorer", "my_test_scorer")

        # /health reflects the reload too.
        assert c.get("/health").json()["plugins"]["loaded"] == ["my_scorer"]
