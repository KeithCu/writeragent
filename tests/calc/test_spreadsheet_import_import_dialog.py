# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import options dialog and conversion helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.calc.spreadsheet_import.import_dialog import (
    _unique_sheet_name,
    fetch_actual_values,
    result_message,
    run_sheet_conversion,
    selection_range_address,
)
from tests.doc_stubs import CalcDocStub, CalcSheetStub, CalcSheetsStub


def test_fetch_actual_values_bulk_read():
    sheet = MagicMock()
    sheet.getCellRangeByPosition.return_value.getDataArray.return_value = (
        ("Q1 2026", 0.0, "", None),
    )
    addresses = ["A1", "B1", "C1", "D1"]

    res = fetch_actual_values(sheet, addresses)

    assert res == {"A1": "Q1 2026", "B1": 0.0, "C1": "", "D1": None}
    sheet.getCellRangeByPosition.assert_called_once_with(0, 0, 3, 0)
    sheet.getCellByPosition.assert_not_called()


def test_fetch_actual_values_empty():
    sheet = MagicMock()
    res = fetch_actual_values(sheet, [])
    assert res == {}
    sheet.getCellRangeByPosition.assert_not_called()


def test_result_message():
    success_title, success_msg = result_message({"summary": "s", "failed_verifications": []})
    assert success_title == "Success"
    assert "Conversion completed successfully!" in success_msg

    mismatch_title, mismatch_msg = result_message({
        "summary": "s",
        "failed_verifications": [{"address": "B2", "message": "expected 10 got 20"}],
    })
    assert mismatch_title == "Conversion finished with mismatches"
    assert "1 cell(s) did not verify." in mismatch_msg
    assert "B2: expected 10 got 20" in mismatch_msg


def test_unique_sheet_name():
    sheets = CalcSheetsStub([CalcSheetStub("PythonImport")])
    assert _unique_sheet_name(sheets, "PythonImport") == "PythonImport 2"
    assert _unique_sheet_name(sheets, "OtherSheet") == "OtherSheet"


def test_run_sheet_conversion_unique_target_sheet():
    sheet1 = CalcSheetStub("Sheet1", data=[[10.0]])
    existing_py_import = CalcSheetStub("PythonImport")
    doc = CalcDocStub(sheets=[sheet1, existing_py_import], active_sheet="Sheet1")
    sheets = doc.getSheets()

    res = run_sheet_conversion(doc, sheet1, scope="selection", verify=False)

    assert res["status"] == "ok"
    assert sheets.hasByName("PythonImport")
    assert sheets.getByName("PythonImport") is existing_py_import
    assert sheets.hasByName("PythonImport 2")


def test_preserve_formats_flag(monkeypatch):
    mock_enrich = MagicMock()
    monkeypatch.setattr(
        "plugin.calc.spreadsheet_import.import_dialog.enrich_number_formats",
        mock_enrich,
    )

    doc = CalcDocStub(data=[[10.0]], selection="A1")
    source_sheet = doc.getSheets().getByIndex(0)

    # preserve_formats=False
    run_sheet_conversion(doc, source_sheet, scope="selection", verify=False, preserve_formats=False)
    mock_enrich.assert_not_called()

    # preserve_formats=True
    run_sheet_conversion(doc, source_sheet, scope="selection", verify=False, preserve_formats=True)
    mock_enrich.assert_called_once()


def test_selection_range_address():
    single = SimpleNamespace(getRangeAddress=lambda: "single")
    assert selection_range_address(single) == "single"

    multi_single = SimpleNamespace(getRangeAddresses=lambda: ["single_in_list"])
    assert selection_range_address(multi_single) == "single_in_list"

    multi_several = SimpleNamespace(getRangeAddresses=lambda: [1, 2])
    with pytest.raises(ValueError, match="one contiguous range"):
        selection_range_address(multi_several)

    non_cell = SimpleNamespace()
    with pytest.raises(ValueError, match="not a shape or object"):
        selection_range_address(non_cell)
