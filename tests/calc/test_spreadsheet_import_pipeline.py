# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import pipeline orchestration and helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.calc.spreadsheet_import.pipeline import (
    _unique_sheet_name,
    fetch_actual_values,
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
        "plugin.calc.spreadsheet_import.pipeline.enrich_number_formats",
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


def test_run_sheet_conversion_restores_formula_on_verification_failure(monkeypatch):
    # A1=10.0, B1="=ABS(A1)" (cached 999.0 -> fails), C1="=ABS(A1)" (cached 10.0 -> passes).
    sheet1 = CalcSheetStub("Sheet1", data=[[10.0, 999.0, 10.0]])
    sheet1.getCellByPosition(1, 0).setFormula("=ABS(A1)")
    sheet1.getCellByPosition(2, 0).setFormula("=ABS(A1)")
    orig_range = sheet1.getCellRangeByPosition(0, 0, 2, 0)
    monkeypatch.setattr(
        sheet1,
        "getCellRangeByPosition",
        lambda *_args: SimpleNamespace(
            getDataArray=lambda: ((10.0, 999.0, 10.0),),
            getFormulaArray=orig_range.getFormulaArray,
            getRangeAddress=orig_range.getRangeAddress,
        ),
    )
    doc = CalcDocStub(sheets=[sheet1], active_sheet="Sheet1", selection="A1:C1")
    doc.calculateAll = MagicMock()
    monkeypatch.setattr(
        "plugin.calc.spreadsheet_import.pipeline.fetch_actual_values",
        lambda _sheet, addrs: {a: 10.0 for a in addrs},
    )

    res = run_sheet_conversion(doc, sheet1, scope="selection", output_mode="new_sheet", vectorize=False, verify=True)

    assert res["status"] == "partial"
    assert len(res["failed_verifications"]) == 1
    assert res["failed_verifications"][0]["address"] == "B1"
    target_sheet = doc.getSheets().getByName("PythonImport")
    assert target_sheet.getCellByPosition(1, 0).getFormula() == "=ABS(A1)"
    assert target_sheet.getCellByPosition(2, 0).getFormula().startswith("=PY(")
    assert res["report"]["converted"] == ["C1"]
    assert "B1" in res["report"]["pass_through"]
    assert {"address": "B1", "reason": "VERIFY_MISMATCH"} in res["report"]["skipped"]
    assert res["report"]["conversion_rate"] == 0.5
    assert "1 / 2" in res["summary"]
    assert doc.calculateAll.call_count == 2


def test_run_sheet_conversion_re_verifies_after_restore(monkeypatch):
    # Pass 1: B1 mismatches and is restored; C1 passes on pass 1 (10.0) but mismatches on pass 2 (20.0)
    # after B1's restore triggers recalculation, so C1 is also restored.
    sheet1 = CalcSheetStub("Sheet1", data=[[10.0, 999.0, 10.0]])
    sheet1.getCellByPosition(1, 0).setFormula("=ABS(A1)")
    sheet1.getCellByPosition(2, 0).setFormula("=ABS(B1)")
    orig_range = sheet1.getCellRangeByPosition(0, 0, 2, 0)
    monkeypatch.setattr(
        sheet1,
        "getCellRangeByPosition",
        lambda *_args: SimpleNamespace(
            getDataArray=lambda: ((10.0, 999.0, 10.0),),
            getFormulaArray=orig_range.getFormulaArray,
            getRangeAddress=orig_range.getRangeAddress,
        ),
    )
    doc = CalcDocStub(sheets=[sheet1], active_sheet="Sheet1", selection="A1:C1")
    doc.calculateAll = MagicMock()
    pass_num = 0

    def _fetch(_sheet, addrs):
        nonlocal pass_num
        pass_num += 1
        val_c1 = 10.0 if pass_num == 1 else 20.0
        return {a: (10.0 if a == "B1" else val_c1) for a in addrs}

    monkeypatch.setattr("plugin.calc.spreadsheet_import.pipeline.fetch_actual_values", _fetch)

    res = run_sheet_conversion(doc, sheet1, scope="selection", output_mode="new_sheet", vectorize=False, verify=True)

    assert res["status"] == "partial"
    assert [m["address"] for m in res["failed_verifications"]] == ["B1", "C1"]
    target_sheet = doc.getSheets().getByName("PythonImport")
    assert target_sheet.getCellByPosition(1, 0).getFormula() == "=ABS(A1)"
    assert target_sheet.getCellByPosition(2, 0).getFormula() == "=ABS(B1)"
    assert res["report"]["converted"] == []
    assert res["report"]["conversion_rate"] == 0.0
    assert doc.calculateAll.call_count == 3
