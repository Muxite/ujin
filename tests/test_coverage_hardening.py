"""Coverage hardening — close gaps in five target modules.

Target → missing lines:
  ujin/adapt/interval.py   38, 55-56
  ujin/adapt/jitter.py     52, 54, 60
  ujin/jobs/__init__.py    48
  ujin/plugins/loader.py   33-34, 42
  ujin/registry.py         64, 70, 113-114, 129, 174-176, 180-184, 211-233, 236-238, 267-269

All tests are offline and deterministic; no live network access.
"""
from __future__ import annotations

import importlib.util
import random
import sys
import types

import pytest

from ujin.adapt import jitter
from ujin.adapt.interval import AdaptiveInterval
from ujin.plugins import load_plugins
from ujin.registry import BuildContext, register


# ── Registry cleanup ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean():
    register.clear_plugins()
    yield
    register.clear_plugins()


# ═══════════════════════════════════════════════════════════════════════════════
# ujin/adapt/interval.py
# ═══════════════════════════════════════════════════════════════════════════════

def test_interval_min_gt_max_raises():
    """Line 38: third __post_init__ validation."""
    with pytest.raises(ValueError, match="min_interval must be <= max_interval"):
        AdaptiveInterval(base=1, min_interval=100, max_interval=10)


def test_interval_reset():
    """Lines 55-56: reset() body."""
    iv = AdaptiveInterval(base=10, min_interval=1, max_interval=1000, grow=2.0)
    iv.next(changed=False)          # now at 20
    assert iv.current == 20
    ret = iv.reset()
    assert ret == 10
    assert iv.current == 10


# ═══════════════════════════════════════════════════════════════════════════════
# ujin/adapt/jitter.py
# ═══════════════════════════════════════════════════════════════════════════════

def test_apply_full():
    """Line 52: apply dispatches to full()."""
    rng = random.Random(0)
    v = jitter.apply(10.0, "full", rng=rng)
    assert 0.0 <= v <= 10.0


def test_apply_equal():
    """Line 54: apply dispatches to equal()."""
    rng = random.Random(0)
    v = jitter.apply(10.0, "equal", rng=rng)
    assert 5.0 <= v <= 10.0


def test_apply_off_is_identity():
    """apply("off") returns d unchanged (same branch as "none")."""
    assert jitter.apply(7.0, "off") == 7.0


def test_apply_unknown_strategy_raises():
    """Line 60: ValueError for unrecognised strategy names."""
    with pytest.raises(ValueError, match="unknown jitter strategy"):
        jitter.apply(5.0, "totally_unknown")


# ═══════════════════════════════════════════════════════════════════════════════
# ujin/jobs/__init__.py
# ═══════════════════════════════════════════════════════════════════════════════

def test_jobs_getattr_unknown_raises():
    """Line 48: AttributeError for names that are not 'JobManager'."""
    import ujin.jobs as _jobs
    with pytest.raises(AttributeError, match="no attribute"):
        _ = _jobs.ThisDoesNotExist


def test_jobs_getattr_jobmanager_works():
    """Confirm the lazy-load happy path still resolves correctly."""
    import ujin.jobs as _jobs
    cls = _jobs.JobManager
    assert cls.__name__ == "JobManager"


# ═══════════════════════════════════════════════════════════════════════════════
# ujin/plugins/loader.py
# ═══════════════════════════════════════════════════════════════════════════════

_PKG_PLUGIN = """\
from ujin import register

@register.source("pkg_src")
def make(cfg):
    class _S:
        key = "pkg_src"
    return _S()
"""


def test_loader_discovers_package_directories(tmp_path):
    """Lines 33-34: directories containing __init__.py are loaded as packages."""
    pkg = tmp_path / "my_plugin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(_PKG_PLUGIN)
    status = load_plugins(str(tmp_path))
    assert "my_plugin" in status["loaded"]
    assert register.has("source", "pkg_src")


def test_loader_ignores_dirs_without_init(tmp_path):
    """_discover skips subdirectories that lack __init__.py."""
    bare = tmp_path / "just_a_dir"
    bare.mkdir()
    (bare / "stuff.py").write_text("x = 1")
    status = load_plugins(str(tmp_path))
    assert status == {"loaded": [], "failed": []}


def test_loader_none_spec_becomes_failure(tmp_path, monkeypatch):
    """Line 42: ImportError raised when spec_from_file_location returns None."""
    monkeypatch.setattr(
        importlib.util, "spec_from_file_location", lambda *a, **kw: None
    )
    (tmp_path / "broken.py").write_text("x = 1")
    status = load_plugins(str(tmp_path))
    assert "broken" in status["failed"]


# ═══════════════════════════════════════════════════════════════════════════════
# ujin/registry.py
# ═══════════════════════════════════════════════════════════════════════════════

# -- decorator surface (lines 64, 70) -----------------------------------------

def test_transform_decorator_and_build():
    """Line 64: register.transform() method."""
    @register.transform("ht_transform")
    def _make(cfg):
        return {"v": cfg.get("v")}

    assert register.has("transform", "ht_transform")
    assert register.build_transform("ht_transform", {"v": 7}) == {"v": 7}


def test_scorer_decorator_and_build():
    """Lines 70, 129: register.scorer() decorator + build_scorer()."""
    @register.scorer("ht_scorer")
    def _make(cfg):
        return cfg.get("threshold", 0.5)

    assert register.has("scorer", "ht_scorer")
    result = register.build_scorer("ht_scorer", {"threshold": 0.9})
    assert result == pytest.approx(0.9)


# -- inspect.signature fallback (lines 113-114) -------------------------------

def test_build_with_uninspectable_factory(monkeypatch):
    """Lines 113-114: nparams falls back to 1 when signature inspection fails."""
    import ujin.registry as _reg

    @register.source("_uninsp")
    def _factory(cfg):
        return "value"

    def _raise(_):
        raise ValueError("no signature available")

    monkeypatch.setattr(_reg.inspect, "signature", _raise)
    result = register.build_source("_uninsp", {})
    assert result == "value"


# -- builtin source: site (lines 174-176) -------------------------------------

def test_build_site_source():
    """Lines 174-176: _src_site factory."""
    p = register.build_source("site", {"url": "http://example.com"})
    assert p.url == "http://example.com"


# -- builtin source: scrape (lines 180-184) -----------------------------------

def test_build_scrape_source_without_service_raises():
    """Lines 180-181: ValueError when scrape_service is absent from context."""
    with pytest.raises(ValueError, match="scrape source needs the scrape backend"):
        register.build_source("scrape", {"url": "http://x"}, BuildContext())


def test_build_scrape_source_with_service():
    """Lines 182-184: happy path — ScrapePollable created with injected service."""
    svc = object()
    p = register.build_source(
        "scrape",
        {"url": "http://x", "mode": "article"},
        BuildContext(scrape_service=svc),
    )
    assert p.url == "http://x"
    assert p._svc is svc


# -- builtin source: amazon_search (lines 211-233) ----------------------------

def test_build_amazon_search_single_term():
    """Lines 211-224, 232: _src_amazon_search single-term path."""
    p = register.build_source("amazon_search", {"term": "laptop"})
    assert hasattr(p, "key")


def test_build_amazon_search_multi_terms():
    """Lines 225-231: _src_amazon_search multi-terms → MultiPollable."""
    p = register.build_source(
        "amazon_search", {"terms": ["laptop", "tablet"], "key": "my_key"}
    )
    assert p.key == "my_key"


# -- builtin source: amazon_category (lines 236-238) -------------------------

def test_build_amazon_category_source():
    """Lines 236-238: _src_amazon_category factory."""
    p = register.build_source("amazon_category", {})
    assert p is not None


# -- builtin source: marketplace_search (lines 267-269) -----------------------

def test_build_marketplace_search_source():
    """Lines 267-269: _src_marketplace_search factory."""
    profiles = {
        "test_mkt": {
            "domain": "example.com",
            "search_url": "https://example.com/search?q={query}",
            "keyterms": {"General": ["widget"]},
        }
    }
    p = register.build_source(
        "marketplace_search",
        {"profile": "test_mkt", "profiles": profiles},
    )
    assert p is not None
