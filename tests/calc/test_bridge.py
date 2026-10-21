# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for CalcBridge sheet-qualified range resolution."""

from unittest.mock import MagicMock

from plugin.calc.bridge import CalcBridge
from plugin.framework.errors import DocumentDisposedError, UnoObjectError
import pytest


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


@pytest.mark.parametrize(
    "value, sheet_name, value_2",
    [
        pytest.param("Summary.A1", "Other", "Other", id="test_resolve_conflict_raises"),
        pytest.param("Summary.B4:B6", "Sources", "Sources", id="test_resolve_disagreeing_prefix_and_sheet_name_raises"),
    ],
)
def test_resolve_conflict_raises(value, sheet_name, value_2):
    doc = MagicMock()
    bridge = CalcBridge(doc)
    try:
        bridge.resolve(value, sheet_name=sheet_name)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Summary" in str(e)
        assert value_2 in str(e)


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

def test_get_sheet_error_lists_available_sheets():
    doc = MagicMock()
    sheets = MagicMock()
    sheets.hasByName.return_value = False
    sheets.getElementNames.return_value = ("Summary", "Sources", "Data")
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    with pytest.raises(UnoObjectError) as exc_info:
        bridge.get_sheet("NonExistent")
    assert "No sheet named 'NonExistent'" in str(exc_info.value)
    assert "Available: Summary, Sources, Data" in str(exc_info.value)


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
    with pytest.raises(UnoObjectError) as exc_info:
        bridge.get_sheet("NonExistent")
    err_msg = str(exc_info.value)
    assert "Available: Summary, Data" in err_msg
    assert "_foo" not in err_msg
    assert "__Anonymous_Sheet_DB__0" not in err_msg
    assert not is_agent_visible_sheet("__Anonymous_Sheet_DB__0")


def test_resolve_range_or_address_disposal_propagates():
    class DisposedException(Exception):
        pass

    doc = MagicMock()
    doc.getCurrentController.side_effect = DisposedException("disposed")
    doc.getSheets.side_effect = DisposedException("disposed")

    bridge = CalcBridge(doc)
    with pytest.raises(DocumentDisposedError):
        bridge.resolve_range_or_address("Total")


def test_resolve_range_or_address_qualified_named_range():
    doc = MagicMock()
    data_sheet = _sheet("Data")
    data_named = MagicMock()
    cells = object()
    data_named.getReferredCells.return_value = cells
    data_sheet.NamedRanges.hasByName.side_effect = lambda n: n == "LocalName"
    data_sheet.NamedRanges.getByName.return_value = data_named

    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda n: n == "Data"
    sheets.getByName.side_effect = lambda n: data_sheet
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    assert bridge.resolve_range_or_address("Data.LocalName") is cells
    data_sheet.NamedRanges.getByName.assert_called_once_with("LocalName")


def test_resolve_range_or_address_global_only_named_range():
    doc = MagicMock()
    global_cells = object()
    global_named = MagicMock()
    global_named.getReferredCells.return_value = global_cells
    doc.NamedRanges.hasByName.side_effect = lambda name: name == "GlobalTotal"
    doc.NamedRanges.getByName.return_value = global_named

    active = _sheet("Sheet1")
    active.NamedRanges.hasByName.return_value = False
    doc.getCurrentController.return_value.getActiveSheet.return_value = active

    bridge = CalcBridge(doc)
    assert bridge.resolve_range_or_address("GlobalTotal") is global_cells
    doc.NamedRanges.getByName.assert_called_once_with("GlobalTotal")


def test_resolve_range_or_address_other_visible_sheet_wins():
    doc = MagicMock()
    doc.NamedRanges.hasByName.return_value = False

    active = _sheet("Sheet1")
    active.NamedRanges.hasByName.return_value = False
    doc.getCurrentController.return_value.getActiveSheet.return_value = active

    sheet2 = _sheet("Sheet2")
    sheet2_named = MagicMock()
    sheet2_cells = object()
    sheet2_named.getReferredCells.return_value = sheet2_cells
    sheet2.NamedRanges.hasByName.side_effect = lambda name: name == "OtherTotal"
    sheet2.NamedRanges.getByName.return_value = sheet2_named

    sheets = MagicMock()
    sheets.getCount.return_value = 2
    sheets.getByIndex.side_effect = lambda idx: [active, sheet2][idx]
    sheets.hasByName.side_effect = lambda name: name in ("Sheet1", "Sheet2")
    sheets.getByName.side_effect = lambda name: {"Sheet1": active, "Sheet2": sheet2}[name]
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    assert bridge.resolve_range_or_address("OtherTotal") is sheet2_cells
    sheet2.NamedRanges.getByName.assert_called_once_with("OtherTotal")


def test_resolve_range_or_address_hidden_sheet_is_not_fallback():
    doc = MagicMock()
    doc.NamedRanges.hasByName.return_value = False

    active = _sheet("Sheet1")
    active.NamedRanges.hasByName.return_value = False
    doc.getCurrentController.return_value.getActiveSheet.return_value = active

    hidden = _sheet("__Anonymous_Sheet_DB__0")
    hidden_named = MagicMock()
    hidden_named.getReferredCells.return_value = object()
    hidden.NamedRanges.hasByName.side_effect = lambda name: name == "InternalName"
    hidden.NamedRanges.getByName.return_value = hidden_named

    sheets = MagicMock()
    sheets.getCount.return_value = 2
    sheets.getByIndex.side_effect = lambda idx: [active, hidden][idx]
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    # Falls through past named-range search to A1 resolve (which raises or uses getCellRangeByName)
    active.getCellRangeByName.return_value = object()
    bridge.resolve_range_or_address("InternalName")
    hidden.NamedRanges.getByName.assert_not_called()


def test_resolve_range_or_address_qualified_missing_does_not_fall_back_to_global():
    doc = MagicMock()
    global_named = MagicMock()
    global_named.getReferredCells.return_value = object()
    doc.NamedRanges.hasByName.side_effect = lambda name: name == "Missing"
    doc.NamedRanges.getByName.return_value = global_named

    data_sheet = _sheet("Data")
    data_sheet.NamedRanges.hasByName.return_value = False

    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda name: name == "Data"
    sheets.getByName.side_effect = lambda name: data_sheet
    sheets.getCount.return_value = 1
    sheets.getByIndex.return_value = data_sheet
    doc.getSheets.return_value = sheets

    bridge = CalcBridge(doc)
    data_sheet.getCellRangeByName.return_value = object()
    bridge.resolve_range_or_address("Data.Missing")
    # Must NOT return or call global named range for "Missing"
    doc.NamedRanges.getByName.assert_not_called()


