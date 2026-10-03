# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Unit tests for Calc named range tools and flag parsing."""

from unittest.mock import MagicMock
import pytest

from plugin.calc.named_ranges import (
    NamedRangeAdd,
    NamedRangeCreateFromTitles,
    NamedRangeDelete,
    NamedRangeEdit,
    NamedRangeGetInfo,
    NamedRangeList,
    _extract_range_info,
    _format_flags,
    _parse_flags,
    _resolve_container,
)


def test_parse_flags_and_format_flags():
    """Verify NamedRangeFlag parsing and formatting."""
    # None or empty
    assert _parse_flags(None) == 0
    assert _parse_flags([]) == 0
    assert _parse_flags("") == 0

    # Int passthrough
    assert _parse_flags(3) == 3

    # String parsing
    assert _parse_flags("print_area") == 2
    assert _parse_flags("filter_criteria, print_area") == 3
    assert _parse_flags(["column_header", "row_header"]) == 12

    # Roundtrip formatting
    assert _format_flags(0) == []
    assert _format_flags(2) == ["print_area"]
    assert set(_format_flags(3)) == {"filter_criteria", "print_area"}
    assert set(_format_flags(12)) == {"column_header", "row_header"}


def test_resolve_container_global_and_sheet():
    """Verify _resolve_container routes to doc.NamedRanges or sheet.NamedRanges."""
    doc = MagicMock()
    global_nr = MagicMock()
    doc.NamedRanges = global_nr

    # Global scope
    container, scope_name, sheet = _resolve_container(doc, "global")
    assert container == global_nr
    assert scope_name == "global"
    assert sheet is None

    container, scope_name, sheet = _resolve_container(doc, None)
    assert container == global_nr
    assert scope_name == "global"

    # Sheet-specific scope
    sheet_obj = MagicMock()
    sheet_nr = MagicMock()
    sheet_obj.NamedRanges = sheet_nr
    sheet_obj.getName.return_value = "Sheet2"

    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda name: name == "Sheet2"
    sheets.getByName.return_value = sheet_obj
    doc.getSheets.return_value = sheets

    container, scope_name, sheet = _resolve_container(doc, "Sheet2")
    assert container == sheet_nr
    assert scope_name == "Sheet2"
    assert sheet == sheet_obj

    # Non-existent sheet
    with pytest.raises(Exception):
        _resolve_container(doc, "NonExistentSheet")


def test_extract_range_info():
    """Verify _extract_range_info properly formats metadata and referred cells."""
    nr = MagicMock()
    nr.getName.return_value = "SalesData"
    nr.getContent.return_value = "$Sheet1.$A$1:$B$10"
    nr.getType.return_value = 2  # print_area

    pos = MagicMock()
    pos.Sheet = 0
    pos.Column = 0
    pos.Row = 0
    nr.getReferencePosition.return_value = pos

    cells = MagicMock()
    addr = MagicMock()
    addr.Sheet = 0
    addr.StartColumn = 0
    addr.StartRow = 0
    addr.EndColumn = 1
    addr.EndRow = 9
    cells.getRangeAddress.return_value = addr
    nr.getReferredCells.return_value = cells

    doc = MagicMock()
    sheet0 = MagicMock()
    sheet0.getName.return_value = "Sheet1"
    doc.getSheets.return_value.getByIndex.return_value = sheet0

    info = _extract_range_info(nr, "global", doc)
    assert info["name"] == "SalesData"
    assert info["scope"] == "global"
    assert info["content"] == "$Sheet1.$A$1:$B$10"
    assert info["flags"] == ["print_area"]
    assert info["base_position"]["address"] == "A1"
    assert info["referred_range"]["address"] == "A1:B10"
    assert info["referred_range"]["rows"] == 10
    assert info["referred_range"]["columns"] == 2


def test_named_range_tools_execution_with_mock():
    """Verify NamedRangeAdd, NamedRangeList, NamedRangeEdit, NamedRangeDelete tools."""
    doc = MagicMock()
    named_ranges = MagicMock()
    doc.NamedRanges = named_ranges
    named_ranges.hasByName.return_value = False
    named_ranges.getElementNames.return_value = ["MyRange"]

    nr_obj = MagicMock()
    nr_obj.getName.return_value = "MyRange"
    nr_obj.getContent.return_value = "$Sheet1.$A$1:$A$5"
    nr_obj.getType.return_value = 0
    nr_obj.getReferencePosition.return_value = MagicMock(Sheet=0, Column=0, Row=0)
    nr_obj.getReferredCells.return_value = None
    named_ranges.getByName.return_value = nr_obj

    ctx = MagicMock()
    ctx.doc = doc

    # 1. Add
    tool_add = NamedRangeAdd()
    res_add = tool_add.execute(ctx, name="MyRange", content="$Sheet1.$A$1:$A$5", flags=["print_area"])
    assert res_add["status"] == "ok"
    named_ranges.addNewByName.assert_called_once()

    # Now named range exists
    named_ranges.hasByName.return_value = True

    # 2. List
    tool_list = NamedRangeList()
    res_list = tool_list.execute(ctx, scope="global")
    assert res_list["status"] == "ok"
    assert len(res_list["result"]) == 1
    assert res_list["result"][0]["name"] == "MyRange"

    # 3. Get Info
    tool_info = NamedRangeGetInfo()
    res_info = tool_info.execute(ctx, name="MyRange")
    assert res_info["status"] == "ok"
    assert res_info["result"]["name"] == "MyRange"

    # 4. Edit
    tool_edit = NamedRangeEdit()
    res_edit = tool_edit.execute(ctx, name="MyRange", content="$Sheet1.$B$1:$B$10")
    assert res_edit["status"] == "ok"
    nr_obj.setContent.assert_called_once_with("$Sheet1.$B$1:$B$10")

    # 5. Delete
    tool_del = NamedRangeDelete()
    res_del = tool_del.execute(ctx, name="MyRange")
    assert res_del["status"] == "ok"
    named_ranges.removeByName.assert_called_once_with("MyRange")

    # 6. Create from Titles
    sheet_mock = MagicMock()
    range_mock = MagicMock()
    range_addr = MagicMock(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=5)
    range_mock.getRangeAddress.return_value = range_addr
    sheet_mock.getCellRangeByPosition.return_value = range_mock

    sheets_mock = MagicMock()
    sheets_mock.hasByName.return_value = False
    sheets_mock.getByIndex.return_value = sheet_mock
    doc.getSheets.return_value = sheets_mock
    doc.getCurrentController.return_value.getActiveSheet.return_value = sheet_mock

    tool_titles = NamedRangeCreateFromTitles()
    res_titles = tool_titles.execute(ctx, range=["A1:B5"], border="top")
    assert res_titles["status"] == "ok"
    named_ranges.addNewFromTitles.assert_called_once()


def test_named_range_tools_error_handling():
    """Verify that all named range tools return _tool_error on failures."""
    doc = MagicMock()
    named_ranges = MagicMock()
    doc.NamedRanges = named_ranges
    named_ranges.hasByName.return_value = False
    doc.getSheets.return_value.hasByName.return_value = False
    doc.getSheets.return_value.getCount.return_value = 0
    doc.getCurrentController.return_value.getActiveSheet.return_value.NamedRanges.hasByName.return_value = False

    ctx = MagicMock()
    ctx.doc = doc

    # 1. GetInfo not found
    tool_info = NamedRangeGetInfo()
    res_info = tool_info.execute(ctx, name="NonExistent")
    assert res_info["status"] == "error"
    assert res_info["code"] == "NAMED_RANGE_NOT_FOUND"

    # 2. Add duplicate
    named_ranges.hasByName.return_value = True
    tool_add = NamedRangeAdd()
    res_add = tool_add.execute(ctx, name="ExistingRange", content="A1")
    assert res_add["status"] == "error"
    assert res_add["code"] == "NAMED_RANGE_EXISTS"

    # 3. Edit not found
    named_ranges.hasByName.return_value = False
    tool_edit = NamedRangeEdit()
    res_edit = tool_edit.execute(ctx, name="NonExistent", content="B1")
    assert res_edit["status"] == "error"
    assert res_edit["code"] == "NAMED_RANGE_NOT_FOUND"

    # 4. Edit rename collision
    named_ranges.hasByName.side_effect = lambda name: name in ("RangeA", "RangeB")
    res_edit_coll = tool_edit.execute(ctx, name="RangeA", new_name="RangeB")
    assert res_edit_coll["status"] == "error"
    assert res_edit_coll["code"] == "NAMED_RANGE_EXISTS"

    # 5. Delete not found
    named_ranges.hasByName.side_effect = None
    named_ranges.hasByName.return_value = False
    tool_del = NamedRangeDelete()
    res_del = tool_del.execute(ctx, name="NonExistent")
    assert res_del["status"] == "error"
    assert res_del["code"] == "NAMED_RANGE_NOT_FOUND"

    # 6. CreateFromTitles invalid border
    tool_titles = NamedRangeCreateFromTitles()
    res_titles = tool_titles.execute(ctx, range=["A1:B5"], border="invalid_border")
    assert res_titles["status"] == "error"
    assert res_titles["code"] == "INVALID_BORDER"

    # 7. CreateFromTitles missing range
    tool_titles = NamedRangeCreateFromTitles()
    res_titles = tool_titles.execute(ctx, range=[])
    assert res_titles["status"] == "error"
    assert res_titles["code"] == "INVALID_ARGUMENT"

    # 8. Add invalid base_cell
    named_ranges.hasByName.side_effect = None
    named_ranges.hasByName.return_value = False
    tool_add = NamedRangeAdd()
    res_add_invalid_base = tool_add.execute(ctx, name="NewRange", content="A1", base_cell="invalid!")
    assert res_add_invalid_base["status"] == "error"
    assert res_add_invalid_base["code"] == "INVALID_BASE_CELL"
