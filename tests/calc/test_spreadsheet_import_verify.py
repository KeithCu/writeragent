# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import verify helpers."""

from __future__ import annotations

from plugin.calc.spreadsheet_import.emit import build_converted_output_model
from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays
from plugin.calc.spreadsheet_import.models import ConversionReport, OutputSheetModel, SheetModel
from plugin.calc.spreadsheet_import.verify import _values_equal, verify_converted_cells


def test_values_equal_float_tolerance():
    ok, _ = _values_equal(1.0, 1.0 + 1e-12, rtol=1e-9)
    assert ok


def test_values_equal_string():
    ok, _ = _values_equal("hello", "hello", rtol=1e-9)
    assert ok


def _build_sample() -> tuple[SheetModel, OutputSheetModel, ConversionReport]:
    data = [
        ["", 1000.0, 0.1, 1100.0],
        ["", "", 1000.0, 1100.0],
    ]
    formulas = [
        ["", "", "", ""],
        ["", "", "=ABS(B2)", "=AVERAGE(B2:D2)"],
    ]
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=1,
        start_row=1,
        data_array=data,
        formula_array=formulas,
    )
    output, report = build_converted_output_model(model)
    return model, output, report


def test_verify_converted_cells_smoke():
    model, output, report = _build_sample()
    actual_values = {addr: model.cells[addr].value for addr in report.converted}
    result = verify_converted_cells(model, output, report, actual_values=actual_values)
    assert len(result.passed) == len(report.converted)
    assert not result.skipped
    assert not result.failed


def test_verify_converted_cells_wrong_actual_fails():
    model, output, report = _build_sample()
    actual_values = {addr: model.cells[addr].value for addr in report.converted}
    bad_addr = report.converted[0]
    actual_values[bad_addr] = -1.0
    result = verify_converted_cells(model, output, report, actual_values=actual_values)
    assert len(result.failed) == 1
    assert result.failed[0].address == bad_addr
    assert len(result.passed) == len(report.converted) - 1


def test_verify_converted_cells_missing_actual_is_skipped():
    model, output, report = _build_sample()
    missing_addr = report.converted[0]
    actual_values = {addr: model.cells[addr].value for addr in report.converted if addr != missing_addr}
    result = verify_converted_cells(model, output, report, actual_values=actual_values)
    assert missing_addr in result.skipped
    assert missing_addr not in result.passed
    assert missing_addr not in [m.address for m in result.failed]


def test_verify_rejects_non_py_formula():
    model, output, report = _build_sample()
    addr = report.converted[0]
    output.cells[addr].formula = "=PYRAMID(1)"
    actual_values = {a: model.cells[a].value for a in report.converted}
    result = verify_converted_cells(model, output, report, actual_values=actual_values)
    assert len(result.failed) == 1
    assert result.failed[0].address == addr
    assert result.failed[0].message == "missing PY formula"


def test_verify_accepts_stored_python_formula():
    model, output, report = _build_sample()
    addr = report.converted[0]
    output.cells[addr].formula = '=PYTHON("result = 1")'
    actual_values = {a: model.cells[a].value for a in report.converted}
    result = verify_converted_cells(model, output, report, actual_values=actual_values)
    assert addr in result.passed
