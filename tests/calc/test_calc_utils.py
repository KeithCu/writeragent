# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.calc.calc_utils — merged cell geometry and sheet resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.calc.calc_utils import (
    EMPTY,
    FORMULA,
    TEXT,
    VALUE,
    cell_type_name,
    cell_value_and_type,
    get_cell_geometry,
    get_cell_geometry_target,
    query_interface,
    resolve_cell_address,
    resolve_sheet,
    resolve_sheet_and_cell,
)
from plugin.framework.errors import DocumentDisposedError, UnoObjectError


class TestGetCellGeometry:
    """get_cell_geometry should collapse merged areas before reading Position/Size."""

    def test_unmerged_cell_returns_own_geometry(self):
        cell = SimpleNamespace(IsMerged=False, Position=(0, 0), Size=(100, 50))
        sheet = MagicMock()
        pos, size = get_cell_geometry(sheet, cell)
        assert pos == (0, 0)
        assert size == (100, 50)
        sheet.createCursorByRange.assert_not_called()

    def test_merged_cell_returns_collapsed_geometry(self):
        cell = SimpleNamespace(IsMerged=True, Position=(0, 0), Size=(100, 50))
        cursor = MagicMock()
        cursor.Position = (0, 0)
        cursor.Size = (300, 100)
        sheet = MagicMock()
        sheet.createCursorByRange.return_value = cursor

        pos, size = get_cell_geometry(sheet, cell)

        sheet.createCursorByRange.assert_called_once_with(cell)
        cursor.collapseToMergedArea.assert_called_once()
        assert pos == (0, 0)
        assert size == (300, 100)

    def test_merged_cell_geometry_target_is_collapsed_cursor(self):
        cell = SimpleNamespace(IsMerged=True, Position=(0, 0), Size=(100, 50))
        cursor = MagicMock()
        sheet = MagicMock()
        sheet.createCursorByRange.return_value = cursor

        target = get_cell_geometry_target(sheet, cell)

        assert target is cursor
        sheet.createCursorByRange.assert_called_once_with(cell)
        cursor.collapseToMergedArea.assert_called_once()

    def test_merged_cell_propagates_exception(self):
        cell = SimpleNamespace(IsMerged=True, Position=(5, 10), Size=(80, 40))
        sheet = MagicMock()
        sheet.createCursorByRange.side_effect = RuntimeError("UNO error")

        with pytest.raises(RuntimeError):
            get_cell_geometry(sheet, cell)

    def test_cell_without_ismerged_attribute(self):
        cell = SimpleNamespace(Position=(1, 2), Size=(10, 20))
        sheet = MagicMock()
        pos, size = get_cell_geometry(sheet, cell)
        assert pos == (1, 2)
        assert size == (10, 20)

    def test_geometry_target_propagates_cursor_failure(self):
        cell = SimpleNamespace(IsMerged=True, Position=(5, 6), Size=(70, 30))
        sheet = MagicMock()
        sheet.createCursorByRange.side_effect = RuntimeError("UNO error")

        with pytest.raises(RuntimeError):
            get_cell_geometry_target(sheet, cell)


class TestSheetAndCellResolution:
    """Tests for resolve_sheet, query_interface, resolve_sheet_and_cell, and resolve_cell_address."""

    def test_query_interface_none_object(self):
        assert query_interface(None, "com.sun.star.sheet.XSheetCondition") is None

    def test_query_interface_object_without_method(self):
        obj = object()
        assert query_interface(obj, "com.sun.star.sheet.XSheetCondition") is None

    def test_resolve_sheet_by_name_success(self):
        mock_sheet = MagicMock()
        mock_sheets = MagicMock()
        mock_sheets.hasByName.return_value = True
        mock_sheets.getByName.return_value = mock_sheet

        doc = MagicMock()
        doc.getSheets.return_value = mock_sheets

        assert resolve_sheet(doc, "Sheet2") is mock_sheet
        mock_sheets.getByName.assert_called_once_with("Sheet2")

    def test_resolve_sheet_by_name_not_found(self):
        mock_sheets = MagicMock()
        mock_sheets.hasByName.return_value = False
        mock_sheets.getElementNames.return_value = ("Sheet1", "Sheet2")

        doc = MagicMock()
        doc.getSheets.return_value = mock_sheets

        with pytest.raises(UnoObjectError, match="No sheet named 'NonExistent'"):
            resolve_sheet(doc, "NonExistent")

    def test_resolve_sheet_and_cell_valid(self):
        mock_sheet = MagicMock()
        doc = MagicMock()
        controller = MagicMock()
        controller.getActiveSheet.return_value = mock_sheet
        doc.getCurrentController.return_value = controller

        resolved = resolve_sheet_and_cell(doc, "B5")
        assert resolved == (mock_sheet, 1, 4)

    def test_resolve_sheet_and_cell_with_prefix(self):
        mock_sheet = MagicMock()
        mock_sheets = MagicMock()
        mock_sheets.hasByName.return_value = True
        mock_sheets.getByName.return_value = mock_sheet

        doc = MagicMock()
        doc.getSheets.return_value = mock_sheets

        resolved = resolve_sheet_and_cell(doc, "Data.C10")
        assert resolved == (mock_sheet, 2, 9)

    def test_resolve_sheet_and_cell_invalid_address(self):
        doc = MagicMock()
        assert resolve_sheet_and_cell(doc, "") is None
        assert resolve_sheet_and_cell(doc, "InvalidAddress") is None
        assert resolve_sheet_and_cell(None, "A1") is None

    def test_resolve_sheet_and_cell_disposal_propagates(self):
        class DisposedException(Exception):
            pass

        doc = MagicMock()
        doc.getCurrentController.side_effect = DisposedException("disposed")
        doc.getSheets.side_effect = DisposedException("disposed")

        with pytest.raises(DocumentDisposedError):
            resolve_sheet_and_cell(doc, "A1")

    def test_resolve_cell_address_valid(self):
        mock_cell = MagicMock()
        mock_cell_addr = MagicMock()
        mock_cell.getCellAddress.return_value = mock_cell_addr

        mock_sheet = MagicMock()
        mock_sheet.getCellByPosition.return_value = mock_cell

        doc = MagicMock()
        controller = MagicMock()
        controller.getActiveSheet.return_value = mock_sheet
        doc.getCurrentController.return_value = controller

        assert resolve_cell_address(doc, "A1") is mock_cell_addr
        mock_sheet.getCellByPosition.assert_called_once_with(0, 0)

    def test_resolve_cell_address_invalid_raises(self):
        doc = MagicMock()
        with pytest.raises(UnoObjectError, match="Cannot resolve cell address"):
            resolve_cell_address(doc, "BadRef")


class TestCellValueAndType:
    def test_empty_returns_none(self):
        cell = MagicMock()
        cell.getType.return_value = 0  # EMPTY
        val, type_name = cell_value_and_type(cell)
        assert val is None
        assert type_name == "empty"

    def test_empty_not_as_none_returns_get_string(self):
        cell = MagicMock()
        cell.getType.return_value = 0  # EMPTY
        cell.getString.return_value = ""
        val, type_name = cell_value_and_type(cell, empty_as_none=False)
        assert val == ""
        assert type_name == "empty"
        cell.getString.assert_called_once()

    def test_value_returns_get_value(self):
        cell = MagicMock()
        cell.getType.return_value = 1  # VALUE
        cell.getValue.return_value = 42.5
        val, type_name = cell_value_and_type(cell)
        assert val == 42.5
        assert type_name == "value"

    def test_text_returns_get_string(self):
        cell = MagicMock()
        cell.getType.return_value = 2  # TEXT
        cell.getString.return_value = "hello"
        val, type_name = cell_value_and_type(cell)
        assert val == "hello"
        assert type_name == "text"

    def test_formula_with_zero_error_uses_formula_cell_result(self):
        cell = MagicMock()
        cell.getType.return_value = 3  # FORMULA
        cell.getError.return_value = 0
        cell.getValue.return_value = 100.0
        val, type_name = cell_value_and_type(cell)
        assert val == 100.0
        assert type_name == "formula"

    def test_formula_with_nonzero_error_returns_get_string(self):
        cell = MagicMock()
        cell.getType.return_value = 3  # FORMULA
        cell.getError.return_value = 502
        cell.getString.return_value = "#VALUE!"
        val, type_name = cell_value_and_type(cell)
        assert val == "#VALUE!"
        assert type_name == "formula"

    def test_formula_error_as_string_false_does_not_touch_get_error(self):
        cell = MagicMock()
        cell.getType.return_value = 3  # FORMULA
        cell.getValue.return_value = 12.0
        val, type_name = cell_value_and_type(cell, formula_error_as_string=False)
        assert val == 12.0
        assert type_name == "formula"
        cell.getError.assert_not_called()

    def test_value_with_enum_type(self):
        from plugin.calc.calc_utils import VALUE
        cell = MagicMock()
        cell.getType.return_value = VALUE
        cell.getValue.return_value = 99.0
        val, type_name = cell_value_and_type(cell)
        assert val == 99.0
        assert type_name == "value"


class TestCellTypeName:
    """cell_type_name maps UNO cell content types to lowercase strings."""

    def test_empty_type(self):
        assert cell_type_name(EMPTY) == "empty"

    def test_value_type(self):
        assert cell_type_name(VALUE) == "value"

    def test_text_type(self):
        assert cell_type_name(TEXT) == "text"

    def test_formula_type(self):
        assert cell_type_name(FORMULA) == "formula"

    def test_unknown_type(self):
        assert cell_type_name(999) == "unknown"
        assert cell_type_name("invalid") == "unknown"

