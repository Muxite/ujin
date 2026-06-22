"""Contact information extraction — ``extract_contacts`` plus the ``contacts``
scrape mode (single-``mode`` and multi-extract ``extracts``).

Offline and deterministic: the parser runs over a corpus fixture
(``tests/fixtures/html/contacts.html``) and inline snippets; the service
paths reuse the duck-typed fakes from ``test_scrape_service.py``.
"""
from __future__ import annotations

import pytest

from ujin.extract import extract_contacts
from ujin.fetch.http import HttpResponse

from test_scrape_service import FakeHttp, FakeObscura, _service

_HOME = "https://contacts.example.com/"


# ── extract_contacts: empty / malformed input ─────────────────────────────────

def test_empty_string_returns_empty_dict():
    assert extract_contacts("") == {}


def test_whitespace_only_returns_empty_dict():
    assert extract_contacts("   \n\t  ") == {}


def test_none_returns_empty_dict():
    assert extract_contacts(None) == {}  # type: ignore[arg-type]


def test_malformed_html_never_raises():
    result = extract_contacts("<html")
    assert isinstance(result, dict)


def test_no_contacts_returns_all_empty_lists():
    html = "<html><body><p>Hello world, no contacts here.</p></body></html>"
    result = extract_contacts(html)
    assert result == {"emails": [], "phones": [], "links": []}


# ── mailto: href → emails ────────────────────────────────────────────────────

def test_mailto_href_extracted_as_email():
    html = '<a href="mailto:alice@example.com">Email</a>'
    result = extract_contacts(html)
    assert "alice@example.com" in result["emails"]


def test_mailto_query_params_stripped():
    html = '<a href="mailto:bob@example.com?subject=Hello&body=Hi">Contact</a>'
    result = extract_contacts(html)
    assert "bob@example.com" in result["emails"]
    assert "?subject" not in result["emails"][0]


def test_multiple_mailto_hrefs_all_collected():
    html = (
        '<a href="mailto:one@x.test">One</a>'
        '<a href="mailto:two@x.test">Two</a>'
    )
    result = extract_contacts(html)
    assert result["emails"] == ["one@x.test", "two@x.test"]


def test_duplicate_mailto_href_deduplicated():
    html = (
        '<a href="mailto:alice@example.com">First</a>'
        '<a href="mailto:alice@example.com">Second</a>'
    )
    result = extract_contacts(html)
    assert result["emails"].count("alice@example.com") == 1


# ── tel: href → phones ───────────────────────────────────────────────────────

def test_tel_href_extracted_as_phone():
    html = '<a href="tel:+15551234567">Call us</a>'
    result = extract_contacts(html)
    assert "+15551234567" in result["phones"]


def test_multiple_tel_hrefs_all_collected():
    html = (
        '<a href="tel:+15551234567">Office</a>'
        '<a href="tel:+15559876543">Fax</a>'
    )
    result = extract_contacts(html)
    assert result["phones"] == ["+15551234567", "+15559876543"]


def test_duplicate_tel_href_deduplicated():
    html = (
        '<a href="tel:+15551234567">Call</a>'
        '<a href="tel:+15551234567">Call again</a>'
    )
    result = extract_contacts(html)
    assert result["phones"].count("+15551234567") == 1


# ── inline email detection ───────────────────────────────────────────────────

def test_inline_email_in_body_text_extracted():
    html = "<html><body><p>Reach us at hello@example.com for support.</p></body></html>"
    result = extract_contacts(html)
    assert "hello@example.com" in result["emails"]


def test_inline_email_not_extracted_from_script_tag():
    html = (
        "<html><body>"
        "<script>var x = 'noreply@skip.example.com';</script>"
        "</body></html>"
    )
    result = extract_contacts(html)
    assert "noreply@skip.example.com" not in result["emails"]


def test_inline_email_not_extracted_from_style_tag():
    html = (
        "<html><head>"
        "<style>/* contact: css@skip.example.com */</style>"
        "</head><body></body></html>"
    )
    result = extract_contacts(html)
    assert "css@skip.example.com" not in result["emails"]


def test_inline_email_deduped_with_mailto_href():
    html = (
        '<a href="mailto:alice@example.com">Alice</a>'
        '<p>Email alice@example.com directly.</p>'
    )
    result = extract_contacts(html)
    assert result["emails"].count("alice@example.com") == 1


# ── inline phone detection ───────────────────────────────────────────────────

def test_inline_international_phone_extracted():
    html = "<html><body><p>Call us: +1 (555) 123-4567 any time.</p></body></html>"
    result = extract_contacts(html)
    assert result["phones"]


def test_inline_nanp_phone_extracted():
    html = "<html><body><p>Reach us at (800) 555-0199 for support.</p></body></html>"
    result = extract_contacts(html)
    assert result["phones"]


def test_inline_phone_not_extracted_from_script_tag():
    html = (
        "<html><body>"
        "<script>var port = 8080; var x = '555-123-4567';</script>"
        "</body></html>"
    )
    result = extract_contacts(html)
    # Any phones found should NOT come from the script
    # (555-123-4567 is inside script, should be absent)
    for p in result["phones"]:
        assert "555" not in p or "123" not in p or p.startswith("+")


# ── social / profile links ───────────────────────────────────────────────────

def test_twitter_link_extracted():
    html = '<a href="https://twitter.com/example">Twitter</a>'
    result = extract_contacts(html)
    assert "https://twitter.com/example" in result["links"]


def test_x_com_link_extracted():
    html = '<a href="https://x.com/example">X</a>'
    result = extract_contacts(html)
    assert "https://x.com/example" in result["links"]


def test_github_link_extracted():
    html = '<a href="https://github.com/example">GitHub</a>'
    result = extract_contacts(html)
    assert "https://github.com/example" in result["links"]


def test_linkedin_link_extracted():
    html = '<a href="https://linkedin.com/in/example">LinkedIn</a>'
    result = extract_contacts(html)
    assert "https://linkedin.com/in/example" in result["links"]


def test_www_prefixed_social_domain_extracted():
    html = '<a href="https://www.github.com/example">GitHub</a>'
    result = extract_contacts(html)
    assert "https://www.github.com/example" in result["links"]


def test_rel_me_link_extracted_regardless_of_domain():
    html = '<a rel="me" href="https://mastodon.social/@user">Mastodon</a>'
    result = extract_contacts(html)
    assert "https://mastodon.social/@user" in result["links"]


def test_internal_link_not_extracted():
    html = (
        '<a href="/about">About</a>'
        '<a href="https://example.com/team">Team</a>'
    )
    result = extract_contacts(html, base_url="https://example.com/")
    assert result["links"] == []


def test_duplicate_social_link_deduplicated():
    html = (
        '<a href="https://twitter.com/example">Twitter</a>'
        '<a href="https://twitter.com/example">Twitter (footer)</a>'
    )
    result = extract_contacts(html)
    assert result["links"].count("https://twitter.com/example") == 1


# ── base_url resolution ───────────────────────────────────────────────────────

def test_relative_social_href_resolved_against_base_url():
    html = '<a rel="me" href="/profiles/user">Profile</a>'
    result = extract_contacts(html, base_url="https://example.com/")
    assert "https://example.com/profiles/user" in result["links"]


def test_absolute_social_href_unchanged_with_base_url():
    html = '<a href="https://github.com/example">GitHub</a>'
    result = extract_contacts(html, base_url="https://other.example.com/")
    assert "https://github.com/example" in result["links"]


def test_social_href_without_base_url_kept_as_is():
    html = '<a rel="me" href="/profiles/user">Profile</a>'
    result = extract_contacts(html)
    assert "/profiles/user" in result["links"]


# ── document order ────────────────────────────────────────────────────────────

def test_emails_preserved_in_document_order():
    html = (
        '<a href="mailto:z@x.test">Z</a>'
        '<a href="mailto:a@x.test">A</a>'
        '<a href="mailto:m@x.test">M</a>'
    )
    result = extract_contacts(html)
    assert result["emails"] == ["z@x.test", "a@x.test", "m@x.test"]


def test_links_preserved_in_document_order():
    html = (
        '<a href="https://twitter.com/z">Z</a>'
        '<a href="https://github.com/a">A</a>'
        '<a href="https://linkedin.com/m">M</a>'
    )
    result = extract_contacts(html)
    assert result["links"] == [
        "https://twitter.com/z",
        "https://github.com/a",
        "https://linkedin.com/m",
    ]


# ── corpus fixture ────────────────────────────────────────────────────────────

def test_corpus_contacts_emails_found(html_corpus):
    result = extract_contacts(html_corpus["contacts"], base_url=_HOME)
    assert "hello@example.com" in result["emails"]
    assert "support@example.com" in result["emails"]
    assert "info@example.com" in result["emails"]


def test_corpus_contacts_email_no_duplicates(html_corpus):
    result = extract_contacts(html_corpus["contacts"], base_url=_HOME)
    assert result["emails"].count("hello@example.com") == 1


def test_corpus_contacts_phones_found(html_corpus):
    result = extract_contacts(html_corpus["contacts"], base_url=_HOME)
    assert result["phones"]


def test_corpus_contacts_social_links_found(html_corpus):
    result = extract_contacts(html_corpus["contacts"], base_url=_HOME)
    hrefs = result["links"]
    assert any("twitter.com" in h for h in hrefs)
    assert any("github.com" in h for h in hrefs)
    assert any("linkedin.com" in h for h in hrefs)


def test_corpus_contacts_social_links_no_duplicates(html_corpus):
    result = extract_contacts(html_corpus["contacts"], base_url=_HOME)
    assert len(result["links"]) == len(set(result["links"]))


def test_corpus_contacts_script_email_not_extracted(html_corpus):
    result = extract_contacts(html_corpus["contacts"], base_url=_HOME)
    assert "noreply@skip.example.com" not in result["emails"]


# ── scrape mode: service paths ────────────────────────────────────────────────

_CONTACTS_HTML = (
    "<html><body>"
    '<a href="mailto:hello@example.com">Email</a>'
    '<a href="tel:+15551234567">Call</a>'
    '<a href="https://twitter.com/example">Twitter</a>'
    "</body></html>"
)
_EXPECTED_CONTACTS = {
    "emails": ["hello@example.com"],
    "phones": ["+15551234567"],
    "links": ["https://twitter.com/example"],
}


def _contacts_service(**kwargs):
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_CONTACTS_HTML, final_url=_HOME)}
    return _service(FakeHttp(routes), **kwargs)


async def test_single_mode_contacts_returns_dict():
    res = await _contacts_service().scrape(_HOME, mode="contacts")
    assert res.kind == "contacts"
    assert res.contacts == _EXPECTED_CONTACTS
    assert res.fingerprint


async def test_single_mode_contacts_parity_with_multi_extract():
    single = await _contacts_service().scrape(_HOME, mode="contacts")
    multi = await _contacts_service().scrape_multi(_HOME, modes=["contacts"])
    assert single.kind == "contacts" == multi["contacts"].kind
    assert single.contacts == multi["contacts"].contacts
    assert single.fingerprint == multi["contacts"].fingerprint


async def test_multi_extract_contacts_and_metadata():
    results = await _contacts_service().scrape_multi(_HOME, modes=["contacts", "metadata"])
    assert set(results) == {"contacts", "metadata"}
    assert results["contacts"].kind == "contacts"
    assert results["contacts"].contacts == _EXPECTED_CONTACTS


async def test_single_mode_contacts_served_from_cache_on_cooldown():
    from ujin.cache import HostPolicy

    svc = _contacts_service(policy=HostPolicy(cooldown_secs=60))
    first = await svc.scrape(_HOME, mode="contacts")
    assert first.kind == "contacts"
    svc._policy.record_failure(_HOME)
    cached = await svc.scrape(_HOME, mode="contacts")
    assert cached.cached is True
    assert cached.kind == "contacts"
    assert cached.contacts == first.contacts


# ── route-level dispatch ──────────────────────────────────────────────────────

def _contacts_app():
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    app = create_scrape_app(ScrapeConfig())
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_CONTACTS_HTML, final_url=_HOME)}
    service = ScrapeService(
        http=FakeHttp(routes), obscura=FakeObscura(),
        cache=ScrapeCache(), policy=HostPolicy(cooldown_secs=60),
        config=ScrapeConfig(fast_path_min_links=1),
    )
    return app, service


def test_route_single_contacts_mode_returns_dict_under_contacts_field():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _contacts_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "mode": "contacts"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "contacts"
        assert body["contacts"] == _EXPECTED_CONTACTS
    finally:
        client.__exit__(None, None, None)


def test_route_multi_extract_contacts_under_extracts():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _contacts_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "modes": ["contacts", "metadata"]})
        assert r.status_code == 200
        body = r.json()
        assert set(body["extracts"]) == {"contacts", "metadata"}
        assert body["extracts"]["contacts"]["kind"] == "contacts"
        assert body["extracts"]["contacts"]["contacts"] == _EXPECTED_CONTACTS
    finally:
        client.__exit__(None, None, None)


def test_route_existing_modes_unaffected_by_contacts_addition():
    """Requesting feeds mode must still work exactly as before."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _contacts_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "mode": "feeds"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "feeds"
        assert body["contacts"] is None
    finally:
        client.__exit__(None, None, None)
