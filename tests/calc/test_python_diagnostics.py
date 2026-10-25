# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Python diagnostics store."""

from __future__ import annotations

from plugin.calc.python.diagnostics import (
    PythonDiagnosticsStore,
    diagnostics_detail_text,
    record_python_eval,
)


def test_record_and_filter_errors_and_output():
    store = PythonDiagnosticsStore(max_entries=10)
    store.record(workbook_key="wb1", code="print(1)", status="ok", stdout="1\n")
    store.record(workbook_key="wb1", code="raise", status="error", message="boom")
    store.record(workbook_key="wb1", code="result=1", status="ok", stdout="")

    errors = store.list_entries("wb1", filt="errors")
    assert len(errors) == 1
    assert errors[0].message == "boom"

    output = store.list_entries("wb1", filt="output")
    assert len(output) == 2

    all_entries = store.list_entries("wb1", filt="all")
    assert len(all_entries) == 3


def test_ring_buffer_bound():
    store = PythonDiagnosticsStore(max_entries=3)
    for i in range(5):
        store.record(workbook_key="wb", code=f"c{i}", status="error", message=str(i))
    entries = store.list_entries("wb", newest_first=False)
    assert [e.message for e in entries] == ["2", "3", "4"]


def test_latest_for_code_and_detail():
    store = PythonDiagnosticsStore()
    store.record(workbook_key="wb", code="result = 1", status="ok", stdout="hi")
    store.record(workbook_key="wb", code="result = 1", status="error", message="fail", address="Sheet1.B2")
    latest = store.latest_for_code("wb", "result = 1")
    assert latest is not None
    assert latest.is_error
    text = diagnostics_detail_text(latest)
    assert "Sheet1.B2" in text
    assert "fail" in text


def test_record_clips_stdout_message_and_traceback():
    store = PythonDiagnosticsStore()
    entry = store.record(
        workbook_key="wb",
        code="print(1)",
        status="error",
        message="m" * 2001,
        stdout="o" * 8001,
        traceback="t" * 8001,
    )
    assert entry.message.startswith("m" * 2000)
    assert "truncated, 1 more characters" in entry.message
    assert entry.stdout.startswith("o" * 8000)
    assert "truncated, 1 more characters" in entry.stdout
    assert entry.traceback.startswith("t" * 8000)
    assert "truncated, 1 more characters" in entry.traceback

    short_entry = store.record(
        workbook_key="wb",
        code="print(1)",
        status="ok",
        message="short",
        stdout="hello",
        traceback="",
    )
    assert "truncated" not in short_entry.message
    assert "truncated" not in short_entry.stdout
    assert "truncated" not in short_entry.traceback


def test_workbook_buckets_are_bounded_and_touched_key_survives():
    store = PythonDiagnosticsStore()
    for i in range(16):
        store.record(workbook_key=f"wb{i}", code="x=1", status="ok")
    store.record(workbook_key="wb0", code="x=2", status="ok")
    store.record(workbook_key="wb16", code="x=3", status="ok")

    assert store.list_entries("wb1") == []
    assert len(store.list_entries("wb0")) > 0
    assert len(store.list_entries("wb16")) > 0


def test_latest_for_code_requires_exact_code():
    store = PythonDiagnosticsStore()
    store.record(workbook_key="wb", code="short_prefix", status="ok", stdout="stored-short")
    assert store.latest_for_code("wb", "short_prefix_and_more_tokens") is None

    store.record(workbook_key="wb2", code="long_stored_code_snippet", status="ok", stdout="stored-long")
    assert store.latest_for_code("wb2", "long_stored") is None

    assert store.latest_for_code("wb", "short_prefix") is not None
    assert store.latest_for_code("wb", "short_prefix").stdout == "stored-short"


def test_module_singleton_record_python_eval():
    entry = record_python_eval(
        workbook_key="singleton-test",
        code="x",
        status="error",
        message="nope",
        stdout="",
    )
    assert entry.status == "error"
    assert "ERROR" in entry.summary_line()


def test_diagnostics_workbook_key_uses_caller_doc_only():
    from unittest.mock import MagicMock, patch
    from plugin.calc.python.diagnostics import diagnostics_workbook_key

    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = "file:///other.ods"

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx") as front_window,
    ):
        assert diagnostics_workbook_key(ctx, doc=doc) == "calc:file:///other.ods"
        # No caller doc: no front-window guess.
        assert diagnostics_workbook_key(ctx, doc=None) == "unknown"
    front_window.assert_not_called()
