"""HTML table extraction — the `extract_tables` parser plus the `tables`
scrape mode (single-`mode` and multi-extract `extracts`).

Offline and deterministic: the parser runs over a new corpus fixture
(`tests/fixtures/html/tables.html`) and inline snippets; the service paths
reuse the duck-typed fakes from test_scrape_service.py.
"""
from __future__ import annotations

import pytest

from ujin.extract import extract_tables
from ujin.fetch.http import HttpResponse

from test_scrape_service import FakeHttp, FakeObscura, _service

_HOME = "https://data.example.com/"


# ── extract_tables: the parser ───────────────────────────────────────────────

def test_acceptance_returns_list_of_dicts_for_multi_table_page(html_corpus):
    rows = extract_tables(html_corpus["tables"])
    assert isinstance(rows, list)
    assert rows and all(isinstance(r, dict) for r in rows)


def test_header_table_keys_rows_by_header_cells(html_corpus):
    rows = extract_tables(html_corpus["tables"])
    # The first (thead) table's rows are keyed by Name/Role/City.
    assert {"Name": "Alice", "Role": "Engineer", "City": "Berlin"} in rows
    assert {"Name": "Carol", "Role": "PM", "City": "Oslo"} in rows


def test_colspan_and_rowspan_expand_into_every_slot(html_corpus):
    rows = extract_tables(html_corpus["tables"])
    # rowspan="2" repeats Q1 down into the South row.
    assert {"Quarter": "Q1", "Region": "North", "Units": "10"} in rows
    assert {"Quarter": "Q1", "Region": "South", "Units": "20"} in rows
    # colspan="2" repeats "Total" across the Quarter + Region columns.
    assert {"Quarter": "Total", "Region": "Total", "Units": "30"} in rows


def test_header_less_table_uses_positional_keys(html_corpus):
    rows = extract_tables(html_corpus["tables"])
    assert {"col0": "r1c0", "col1": "r1c1"} in rows
    assert {"col0": "r2c0", "col1": "r2c1"} in rows


def test_nested_table_is_parsed_separately_not_inlined(html_corpus):
    rows = extract_tables(html_corpus["tables"])
    # The nested table contributes its own row...
    assert {"Key": "depth", "Value": "nested"} in rows
    # ...and its text does NOT leak into the enclosing cell, which keeps only
    # its own inline text ("see notes below").
    overview = next(r for r in rows if r.get("Section") == "Overview")
    assert overview["Detail"] == "see notes below"
    assert "depth" not in overview["Detail"]
    assert "nested" not in overview["Detail"]


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "<html><body><p>no tables here</p></body></html>",
    "<table>",                                      # unclosed
    "<table><tr><td colspan='oops'>x</td></tr>",    # bad colspan, unclosed
    "<table><tr><td rowspan='-5'>y</td></tr></table>",
])
def test_malformed_or_empty_never_raises(bad):
    out = extract_tables(bad)
    assert isinstance(out, list)


def test_table_with_no_cells_yields_nothing():
    # Rows present but cell-less → no grid, no rows (and no crash).
    assert extract_tables("<table><tr></tr><tr></tr></table>") == []


def test_blank_rows_between_data_are_dropped():
    rows = extract_tables(
        "<table>"
        "<tr><th>A</th></tr>"
        "<tr></tr>"            # blank spacer row → dropped
        "<tr><td>x</td></tr>"
        "</table>"
    )
    assert rows == [{"A": "x"}]


def test_duplicate_header_labels_are_disambiguated():
    rows = extract_tables(
        "<table><tr><th>X</th><th>X</th></tr><tr><td>1</td><td>2</td></tr></table>"
    )
    assert rows == [{"X": "1", "X_2": "2"}]


def test_empty_header_cell_falls_back_to_positional_key():
    rows = extract_tables(
        "<table><tr><th>A</th><th></th></tr><tr><td>1</td><td>2</td></tr></table>"
    )
    assert rows == [{"A": "1", "col1": "2"}]


def test_th_only_first_column_is_treated_as_header_row():
    # A first row that mixes <th> and <td> still counts as a header row
    # (it carries a <th>), so subsequent rows key off it.
    rows = extract_tables(
        "<table>"
        "<tr><th>k</th><td>v</td></tr>"
        "<tr><td>a</td><td>b</td></tr>"
        "</table>"
    )
    assert rows == [{"k": "a", "v": "b"}]


# ── scrape mode: multi-extract `extracts` (the headline acceptance path) ─────

_TABLE_HTML = (
    "<html><body>"
    "<table><tr><th>Sym</th><th>Price</th></tr>"
    "<tr><td>AAA</td><td>1.20</td></tr>"
    "<tr><td>BBB</td><td>3.40</td></tr></table>"
    "</body></html>"
)


def _tables_service():
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_TABLE_HTML, final_url=_HOME)}
    return _service(FakeHttp(routes))


async def test_multi_extract_returns_tables_entry():
    svc = _tables_service()
    results = await svc.scrape_multi(_HOME, modes=["tables", "structured"])
    assert set(results) == {"tables", "structured"}
    tbl = results["tables"]
    assert tbl.kind == "tables"
    assert tbl.tables == [
        {"Sym": "AAA", "Price": "1.20"},
        {"Sym": "BBB", "Price": "3.40"},
    ]
    assert tbl.fingerprint  # sha256 over the rows


async def test_single_mode_tables_parity_with_multi_extract():
    single = await _tables_service().scrape(_HOME, mode="tables")
    multi = await _tables_service().scrape_multi(_HOME, modes=["tables"])
    assert single.kind == "tables" == multi["tables"].kind
    assert single.tables == multi["tables"].tables
    assert single.fingerprint == multi["tables"].fingerprint


async def test_single_mode_tables_served_from_cache_on_cooldown():
    from ujin.cache import HostPolicy

    svc = _service(FakeHttp({_HOME: HttpResponse(url=_HOME, status=200,
                                                 body=_TABLE_HTML, final_url=_HOME)}),
                   policy=HostPolicy(cooldown_secs=60))
    first = await svc.scrape(_HOME, mode="tables")
    assert first.kind == "tables"
    svc._policy.record_failure(_HOME)  # arm the cooldown
    cached = await svc.scrape(_HOME, mode="tables")
    assert cached.cached is True
    assert cached.kind == "tables"
    assert cached.tables == first.tables


# ── route-level dispatch ─────────────────────────────────────────────────────

def test_route_tables_mode_returns_rows_under_extracts():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    app = create_scrape_app(ScrapeConfig())
    client = TestClient(app)
    client.__enter__()
    try:
        routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_TABLE_HTML, final_url=_HOME)}
        app.state.service = ScrapeService(
            http=FakeHttp(routes), obscura=FakeObscura(),
            cache=ScrapeCache(), policy=HostPolicy(cooldown_secs=60),
            config=ScrapeConfig(fast_path_min_links=1),
        )
        r = client.post("/scrape", json={"url": _HOME, "modes": ["tables", "structured"]})
        assert r.status_code == 200
        body = r.json()
        assert set(body["extracts"]) == {"tables", "structured"}
        assert body["extracts"]["tables"]["kind"] == "tables"
        assert body["extracts"]["tables"]["tables"] == [
            {"Sym": "AAA", "Price": "1.20"},
            {"Sym": "BBB", "Price": "3.40"},
        ]
    finally:
        client.__exit__(None, None, None)
