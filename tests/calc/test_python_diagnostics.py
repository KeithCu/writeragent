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


def test_diagnostics_workbook_key_does_not_mutate_session_state():
    from unittest.mock import MagicMock, patch
    from plugin.calc.python.function import _diagnostics_workbook_key
    from plugin.scripting.session_manager import (
        clear_active_calc_session,
        get_cached_calc_session_id,
        record_active_calc_session,
        recorded_calc_session_ids,
    )

    clear_active_calc_session()
    try:
        record_active_calc_session("calc:file:///initial.ods")
        initial_cached = get_cached_calc_session_id()
        initial_recorded = set(recorded_calc_session_ids())

        ctx = MagicMock()
        doc = MagicMock()
        doc.getURL.return_value = "file:///other.ods"

        with patch("plugin.framework.thread_guard.on_main_thread", return_value=True):
            key = _diagnostics_workbook_key(ctx, doc=doc)
            assert key == "calc:file:///other.ods"
            # Must not change cached session id or recorded session ids
            assert get_cached_calc_session_id() == initial_cached
            assert set(recorded_calc_session_ids()) == initial_recorded
    finally:
        clear_active_calc_session()
