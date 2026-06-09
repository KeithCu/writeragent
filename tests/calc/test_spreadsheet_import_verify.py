# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import verify helpers."""

from __future__ import annotations

from plugin.calc.spreadsheet_import.emit import build_converted_output_model
from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays
from plugin.calc.spreadsheet_import.verify import verify_converted_cells, _values_equal


def test_values_equal_float_tolerance():
    ok, _ = _values_equal(1.0, 1.0 + 1e-12, rtol=1e-9)
    assert ok


def test_values_equal_string():
    ok, _ = _values_equal("hello", "hello", rtol=1e-9)
    assert ok


def test_verify_converted_cells_smoke():
    data = [
        ["", 1000.0, 0.1, 1100.0],
        ["", "", "=B2*0.1", "=B2+C2"],
    ]
    formulas = [
        ["", "", "", ""],
        ["", "", "=B2*0.1", "=B2+C2"],
    ]
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=1,
        start_row=1,
        data_array=data,
        formula_array=formulas,
    )
    output, report = build_converted_output_model(model)
    result = verify_converted_cells(model, output, report)
    assert len(result.passed) == len(report.converted)
    assert not result.failed
