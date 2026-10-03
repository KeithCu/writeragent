# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for CalcBridge sheet-qualified range resolution."""

from unittest.mock import MagicMock

from plugin.calc.bridge import CalcBridge


def _sheet(name: str) -> MagicMock:
    sheet = MagicMock()
    sheet.getName.return_value = name
    return sheet


def test_resolve_prefix_wins_over_sheet_name():
    doc = MagicMock()
    summary = _sheet("Summary")
    other = _sheet("Other")
    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda n: n in ("Summary", "Other")
    sheets.getByName.side_effect = lambda n: summary if n == "Summary" else other
    sheets.getElementNames.return_value = ("Summary", "Other")
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    sheet, address = bridge.resolve("Summary.D4:D6", sheet_name=None)
    assert sheet is summary
    assert address == "D4:D6"


def test_resolve_conflict_raises():
    doc = MagicMock()
    bridge = CalcBridge(doc)
    try:
        bridge.resolve("Summary.A1", sheet_name="Other")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Summary" in str(e)
        assert "Other" in str(e)


def test_get_cell_range_honours_sheet_prefix():
    doc = MagicMock()
    data = _sheet("Data")
    active = _sheet("Active")
    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda n: n == "Data"
    sheets.getByName.return_value = data
    sheets.getElementNames.return_value = ("Data", "Active")
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    bridge.get_cell_range(active, "Data.A1:B2")
    data.getCellRangeByPosition.assert_called_once_with(0, 0, 1, 1)
    active.getCellRangeByPosition.assert_not_called()


def test_resolve_disagreeing_prefix_and_sheet_name_raises():
    doc = MagicMock()
    bridge = CalcBridge(doc)
    try:
        bridge.resolve("Summary.B4:B6", sheet_name="Sources")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Summary" in str(e)
        assert "Sources" in str(e)


def test_get_sheet_error_lists_available_sheets():
    doc = MagicMock()
    sheets = MagicMock()
    sheets.hasByName.return_value = False
    sheets.getElementNames.return_value = ("Summary", "Sources", "Data")
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    try:
        bridge.get_sheet("NonExistent")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "No sheet named 'NonExistent'" in str(e)
        assert "Available: Summary, Sources, Data" in str(e)


def test_resolve_range_or_address_unqualified_name_prefers_sheet_local():
    """A bare name defined in both scopes returns the active sheet's cells.

    Calc shadows a workbook name with the same sheet-local name. Checking
    doc.NamedRanges first returned the global referred cells.
    """
    doc = MagicMock()
    global_cells = object()
    local_cells = object()

    global_named = MagicMock()
    global_named.getReferredCells.return_value = global_cells
    doc.NamedRanges.hasByName.side_effect = lambda name: name == "Total"
    doc.NamedRanges.getByName.return_value = global_named

    active = _sheet("Sheet1")
    local_named = MagicMock()
    local_named.getReferredCells.return_value = local_cells
    active.NamedRanges.hasByName.side_effect = lambda name: name == "Total"
    active.NamedRanges.getByName.return_value = local_named
    doc.getCurrentController.return_value.getActiveSheet.return_value = active

    bridge = CalcBridge(doc)
    assert bridge.resolve_range_or_address("Total") is local_cells
    doc.NamedRanges.getByName.assert_not_called()


def test_get_sheet_error_omits_leading_underscore_sheets():
    from plugin.calc.bridge import is_agent_visible_sheet

    doc = MagicMock()
    sheets = MagicMock()
    sheets.hasByName.return_value = False
    sheets.getElementNames.return_value = (
        "Summary",
        "_foo",
        "__Anonymous_Sheet_DB__0",
        "Data",
    )
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    try:
        bridge.get_sheet("NonExistent")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Available: Summary, Data" in str(e)
        assert "_foo" not in str(e)
        assert "__Anonymous_Sheet_DB__0" not in str(e)
    assert not is_agent_visible_sheet("__Anonymous_Sheet_DB__0")

