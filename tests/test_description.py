"""Detail-page description extraction (no network)."""
from ujin.extract.product import extract_description


def test_extracts_feature_bullets():
    html = (
        '<html><body><div id="feature-bullets"><ul>'
        '<li><span class="a-list-item">Wireless over-ear headphones</span></li>'
        '<li><span class="a-list-item">30-hour battery life</span></li>'
        '</ul></div></body></html>'
    )
    d = extract_description(html, source="amazon")
    assert d and "Wireless over-ear headphones" in d and "30-hour battery life" in d


def test_falls_back_to_meta_description():
    html = '<html><head><meta name="description" content="A great little gadget."></head><body></body></html>'
    assert extract_description(html, source="amazon") == "A great little gadget."


def test_returns_none_when_nothing():
    assert extract_description("<html><body><p>nope</p></body></html>", source="amazon") is None


def test_truncates_to_max_chars():
    html = f'<html><head><meta name="description" content="{"x" * 1000}"></head></html>'
    out = extract_description(html, source="amazon", max_chars=50)
    assert out is not None and len(out) == 50
