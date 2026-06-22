"""CsvSink — append event rows to a CSV file (pure stdlib).

Covers header-on-create, explicit vs inferred columns, dict vs list payloads,
stable columns across appends, delimiter/header config, non-dict skipping, the
no-rows no-op, and that it builds through the registry like JobManager wires it.
"""
from __future__ import annotations

import csv as _csv

from ujin.jobs.sinks import CsvSink, build_sink
from ujin.registry import BuildContext, register


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(_csv.reader(fh))


async def test_inferred_columns_and_header_on_create(tmp_path):
    p = tmp_path / "out.csv"
    sink = CsvSink({"path": str(p)})
    await sink.emit({"payload": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]})
    rows = _read(p)
    assert rows[0] == ["a", "b"]              # inferred header
    assert rows[1] == ["1", "2"]
    assert rows[2] == ["3", "4"]


async def test_explicit_columns_order_and_extras_ignored(tmp_path):
    p = tmp_path / "out.csv"
    sink = CsvSink({"path": str(p), "columns": ["b", "a"]})
    await sink.emit({"payload": [{"a": 1, "b": 2, "c": 9}]})
    rows = _read(p)
    assert rows[0] == ["b", "a"]              # declared order, "c" dropped
    assert rows[1] == ["2", "1"]


async def test_dict_payload_single_row(tmp_path):
    p = tmp_path / "out.csv"
    await CsvSink({"path": str(p)}).emit({"payload": {"x": "v"}})
    rows = _read(p)
    assert rows == [["x"], ["v"]]


async def test_header_suppressed(tmp_path):
    p = tmp_path / "out.csv"
    await CsvSink({"path": str(p), "header": False, "columns": ["a"]}).emit(
        {"payload": [{"a": 1}]}
    )
    assert _read(p) == [["1"]]


async def test_columns_stable_across_appends(tmp_path):
    p = tmp_path / "out.csv"
    sink = CsvSink({"path": str(p)})
    await sink.emit({"payload": [{"a": 1, "b": 2}]})   # locks columns to [a, b]
    await sink.emit({"payload": [{"b": 5, "a": 4, "c": 9}]})  # c is ignored
    rows = _read(p)
    assert rows[0] == ["a", "b"]
    assert rows[1] == ["1", "2"]
    assert rows[2] == ["4", "5"]               # ordered by locked columns


async def test_missing_keys_become_empty(tmp_path):
    p = tmp_path / "out.csv"
    sink = CsvSink({"path": str(p), "columns": ["a", "b"]})
    await sink.emit({"payload": [{"a": 1}]})
    assert _read(p) == [["a", "b"], ["1", ""]]


async def test_custom_delimiter_and_path_in_event(tmp_path):
    p = tmp_path / "out.tsv"
    sink = CsvSink({"path": str(p), "delimiter": "\t", "path_in_event": "payload.rows"})
    await sink.emit({"payload": {"rows": [{"a": 1, "b": 2}]}})
    with open(p, encoding="utf-8") as fh:
        text = fh.read()
    assert "a\tb" in text and "1\t2" in text


async def test_non_dict_items_skipped_but_dict_rows_written(tmp_path):
    p = tmp_path / "out.csv"
    await CsvSink({"path": str(p), "columns": ["a"]}).emit(
        {"payload": [{"a": 1}, "scalar", 5, {"a": 2}]}
    )
    assert _read(p) == [["a"], ["1"], ["2"]]


async def test_no_rows_is_a_noop(tmp_path):
    p = tmp_path / "out.csv"
    sink = CsvSink({"path": str(p)})
    await sink.emit({"payload": []})           # empty list
    await sink.emit({"payload": 42})           # scalar -> no rows
    await sink.emit({"payload": "text"})       # string -> no rows
    assert not p.exists()                      # nothing written, file never created


async def test_appends_to_existing_nonempty_file_without_new_header(tmp_path):
    p = tmp_path / "out.csv"
    p.write_text("a\n1\n", encoding="utf-8")   # pre-existing content
    await CsvSink({"path": str(p), "columns": ["a"]}).emit({"payload": [{"a": 2}]})
    assert _read(p) == [["a"], ["1"], ["2"]]    # no duplicate header row


def test_csv_builds_through_registry():
    sink = register.build_sink("csv", {"path": "/tmp/ujin-test.csv"}, BuildContext())
    assert isinstance(sink, CsvSink)
    # also reachable via build_sink directly
    assert isinstance(build_sink("csv", {"path": "/tmp/x.csv"}), CsvSink)
