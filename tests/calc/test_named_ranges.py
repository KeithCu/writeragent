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

import sys
from unittest.mock import MagicMock
import pytest

from plugin.calc.named_ranges import (
    NamedRangeAdd,
    NamedRangeCreateFromTitles,
    NamedRangeDelete,
    NamedRangeEdit,
    NamedRangeGetInfo,
    NamedRangeList,
    _defined_name_error,
    _extract_range_info,
    _format_flags,
    _parse_base_address,
    _parse_flags,
    _resolve_container,
)


def _doc_with_sheet_names(*names: str) -> MagicMock:
    sheet_objs = []
    for name in names:
        sheet = MagicMock()
        sheet.getName.return_value = name
        sheet_objs.append(sheet)
    sheets = MagicMock()
    sheets.getCount.return_value = len(sheet_objs)
    sheets.getByIndex.side_effect = lambda idx: sheet_objs[idx]
    doc = MagicMock()
    doc.getSheets.return_value = sheets
    return doc


class _RecordingCellAddress:
    """CellAddress stand-in that keeps the fields the parser passes.

    The full suite imports ``tests/framework/test_errors.py``, which replaces
    ``sys.modules['com.sun.star.table']`` with a MagicMock. ``CellAddress(Sheet=1)``
    then returns a mock whose ``.Sheet`` is not 1, so a correct index looked
    like a failure.
    """

    def __init__(self, Sheet: int = 0, Column: int = 0, Row: int = 0) -> None:
        self.Sheet = Sheet
        self.Column = Column
        self.Row = Row


def test_named_range_unknown_sheet_prefix_errors(monkeypatch):
    # An unknown prefix used to leave the default sheet (0), so a relative
    # name was anchored on the wrong sheet.
    table = sys.modules.get("com.sun.star.table")
    if table is None:
        table = MagicMock()
        monkeypatch.setitem(sys.modules, "com.sun.star.table", table)
    monkeypatch.setattr(table, "CellAddress", _RecordingCellAddress, raising=False)

    doc = _doc_with_sheet_names("Sheet1", "Data")

    with pytest.raises(ValueError, match="No sheet named 'Nope'"):
        _parse_base_address(doc, "Nope.B2", default_sheet_idx=0)
    with pytest.raises(ValueError, match="No sheet named 'Missing Sheet'"):
        _parse_base_address(doc, "'Missing Sheet'!A1", default_sheet_idx=0)

    pos = _parse_base_address(doc, "Data.B2", default_sheet_idx=0)
    assert pos.Sheet == 1
    assert (pos.Column, pos.Row) == (1, 1)

    bare = _parse_base_address(doc, "C3", default_sheet_idx=4)
    assert bare.Sheet == 4
    assert (bare.Column, bare.Row) == (2, 2)


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
    # Unqualified lookup checks the active sheet before the workbook container.
    # This sheet has no local names, so get/edit/delete stay on doc.NamedRanges.
    active_sheet = MagicMock()
    active_sheet.getName.return_value = "Sheet1"
    active_sheet.NamedRanges.hasByName.return_value = False
    doc.getCurrentController.return_value.getActiveSheet.return_value = active_sheet

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


def test_defined_name_matches_calc_is_name_valid():
    """ScRangeData::IsNameValid rejects cell refs, dots, leading digits, and spaces."""
    for illegal in ("A1", "a1", "AA10", "Q1", "R1C1", "RC", "C1", "R2", "Sales.2026", "1abc", "My Range", "", "A1:B2", "$A$1"):
        assert _defined_name_error(illegal), illegal
    assert "cell" in (_defined_name_error("A1") or "").lower()
    assert "underscore" in (_defined_name_error("Sales.2026") or "").lower()
    assert "underscore" in (_defined_name_error("1abc") or "").lower()
    for legal in ("TaxRate", "_Hidden", "Sales_2026", "Q1Sales", "DATA1", "R2D2", "Sales?", "Ångström"):
        assert _defined_name_error(legal) is None, legal


def _nr(name: str, content: str) -> MagicMock:
    nr = MagicMock()
    nr.getName.return_value = name
    nr.getContent.return_value = content
    nr.getType.return_value = 0
    nr.getReferencePosition.return_value = MagicMock(Sheet=0, Column=0, Row=0)
    nr.getReferredCells.return_value = None
    return nr


def _named_container(items: dict[str, MagicMock]) -> MagicMock:
    container = MagicMock()
    container.getElementNames.return_value = list(items)
    container.hasByName.side_effect = lambda n: n in items
    container.getByName.side_effect = lambda n: items[n]
    return container


def _sheet(name: str, items: dict[str, MagicMock]) -> MagicMock:
    sheet = MagicMock()
    sheet.getName.return_value = name
    sheet.NamedRanges = _named_container(items)
    return sheet


def _workbook(active_name: str, sheets: list[MagicMock], global_items: dict[str, MagicMock]) -> MagicMock:
    doc = MagicMock()
    doc.NamedRanges = _named_container(global_items)
    by_name = {sheet.getName(): sheet for sheet in sheets}
    sheets_obj = MagicMock()
    sheets_obj.getCount.return_value = len(sheets)
    sheets_obj.getByIndex.side_effect = lambda i: sheets[i]
    sheets_obj.hasByName.side_effect = lambda n: n in by_name
    sheets_obj.getByName.side_effect = lambda n: by_name[n]
    doc.getSheets.return_value = sheets_obj
    doc.getCurrentController.return_value.getActiveSheet.return_value = by_name[active_name]
    ctx = MagicMock()
    ctx.doc = doc
    return ctx


def test_edit_rejects_illegal_rename_before_mutation():
    """Illegal new names never reach setName, and earlier fields stay unchanged."""
    nr = _nr("TaxRate", "$Sheet1.$A$1")
    ctx = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr})
    tool = NamedRangeEdit()
    res = tool.execute(ctx, name="TaxRate", new_name="A1", content="$Sheet1.$B$1")
    assert res["status"] == "error"
    assert res["code"] == "INVALID_NAME"
    nr.setName.assert_not_called()
    nr.setContent.assert_not_called()

    res_space = tool.execute(ctx, name="TaxRate", new_name="Sales 2026", content="$Sheet1.$B$1")
    assert res_space["code"] == "INVALID_NAME"
    nr.setContent.assert_not_called()

    res_base = tool.execute(ctx, name="TaxRate", new_name="TaxRate2", base_cell="invalid!")
    assert res_base["code"] == "INVALID_BASE_CELL"
    nr.setName.assert_not_called()


def test_edit_renames_before_other_fields_and_rolls_back():
    """setName runs first. A later setter failure restores the previous name."""
    nr = _nr("TaxRate", "$Sheet1.$A$1")
    ctx = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr})
    order: list[tuple[str, str]] = []
    nr.setName.side_effect = lambda new: order.append(("name", new))
    nr.setContent.side_effect = lambda content: order.append(("content", content))
    nr.setType.side_effect = lambda mask: order.append(("type", str(mask)))

    res = NamedRangeEdit().execute(ctx, name="TaxRate", new_name="TaxRate2", content="$Sheet1.$B$1", flags=["print_area"])
    assert res["status"] == "ok"
    assert [step[0] for step in order] == ["name", "content", "type"]
    assert order[0] == ("name", "TaxRate2")

    nr_fail = _nr("TaxRate", "$Sheet1.$A$1")
    ctx_fail = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr_fail})
    nr_fail.setContent.side_effect = RuntimeError("content rejected")
    res_fail = NamedRangeEdit().execute(ctx_fail, name="TaxRate", new_name="TaxRate2", content="$Sheet1.$B$1")
    assert res_fail["status"] == "error"
    assert res_fail["code"] == "NAMED_RANGE_ERROR"
    assert [call.args[0] for call in nr_fail.setName.call_args_list] == ["TaxRate2", "TaxRate"]


def test_unqualified_name_prefers_sheet_local_shadow():
    """A bare name on a sheet with a local same-spelled range targets the local one."""
    local = _nr("Total", "local")
    glob = _nr("Total", "global")
    sheet = _sheet("Sheet1", {"Total": local})
    ctx = _workbook("Sheet1", [sheet], {"Total": glob})

    info = NamedRangeGetInfo().execute(ctx, name="Total")
    assert info["status"] == "ok"
    assert info["result"]["scope"] == "Sheet1"
    assert info["result"]["content"] == "local"

    forced = NamedRangeGetInfo().execute(ctx, name="Total", scope="global")
    assert forced["result"]["scope"] == "global"
    assert forced["result"]["content"] == "global"

    edit = NamedRangeEdit().execute(ctx, name="Total", content="$Sheet1.$C$1")
    assert edit["status"] == "ok"
    local.setContent.assert_called_once_with("$Sheet1.$C$1")
    glob.setContent.assert_not_called()

    glob_edit = NamedRangeEdit().execute(ctx, name="Total", scope="global", content="$Sheet1.$D$1")
    assert glob_edit["status"] == "ok"
    glob.setContent.assert_called_once_with("$Sheet1.$D$1")

    deleted = NamedRangeDelete().execute(ctx, name="Total")
    sheet.NamedRanges.removeByName.assert_called_once_with("Total")
    ctx.doc.NamedRanges.removeByName.assert_not_called()
    assert deleted["status"] == "ok"

    deleted_global = NamedRangeDelete().execute(ctx, name="Total", scope="global")
    ctx.doc.NamedRanges.removeByName.assert_called_once_with("Total")
    assert deleted_global["status"] == "ok"


def test_list_all_and_get_info_skip_hidden_sheets():
    """scope=all and the unqualified fallback omit _-prefixed generated sheets."""
    visible = _nr("Visible", "v")
    hidden = _nr("Secret", "s")
    other = _nr("Other", "o")
    anon = _nr("Anon", "a")
    sheet1 = _sheet("Sheet1", {"Visible": visible})
    hidden_sheet = _sheet("_hidden", {"Secret": hidden})
    anon_sheet = _sheet("__Anonymous_Sheet_DB__0", {"Anon": anon})
    sheet2 = _sheet("Sheet2", {"Other": other})
    ctx = _workbook("Sheet1", [sheet1, hidden_sheet, anon_sheet, sheet2], {"Global": _nr("Global", "g")})

    listed = NamedRangeList().execute(ctx, scope="all")
    assert listed["status"] == "ok"
    found = {(item["scope"], item["name"]) for item in listed["result"]}
    assert found == {("global", "Global"), ("Sheet1", "Visible"), ("Sheet2", "Other")}

    missing = NamedRangeGetInfo().execute(ctx, name="Secret")
    assert missing["status"] == "error"
    assert missing["code"] == "NAMED_RANGE_NOT_FOUND"

    explicit = NamedRangeGetInfo().execute(ctx, name="Secret", scope="_hidden")
    assert explicit["status"] == "ok"
    assert explicit["result"]["scope"] == "_hidden"
    assert explicit["result"]["content"] == "s"

    fallback = NamedRangeGetInfo().execute(ctx, name="Other")
    assert fallback["status"] == "ok"
    assert fallback["result"]["scope"] == "Sheet2"
