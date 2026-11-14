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
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from com.sun.star.uno import RuntimeException

from plugin.app import get_tools
from plugin.framework.errors import UnoObjectError
from plugin.tests.testing_utils import FakeUndoManager
from plugin.calc.named_ranges import (
    _defined_name_error,
    _extract_range_info,
    _format_flags,
    _parse_base_address,
    _parse_flags,
    _resolve_container,
)
from plugin.framework.tool import prepare_tool_call


def _tool(name: str):
    return get_tools().get(name)


def _doc_with_sheet_names(*names: str) -> MagicMock:
    by_name = {}
    sheet_objs = []
    for idx, name in enumerate(names):
        sheet = MagicMock()
        sheet.getName.return_value = name
        addr = MagicMock()
        addr.Sheet = idx
        sheet.getRangeAddress.return_value = addr
        sheet_objs.append(sheet)
        by_name[name] = sheet
    sheets = MagicMock()
    sheets.getCount.return_value = len(sheet_objs)
    sheets.getByIndex.side_effect = lambda idx: sheet_objs[idx]
    sheets.hasByName.side_effect = lambda n: n in by_name
    sheets.getByName.side_effect = lambda n: by_name[n]
    sheets.getElementNames.return_value = names
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

    with pytest.raises(UnoObjectError, match="No sheet named 'Nope'. Available: Sheet1, Data"):
        _parse_base_address(doc, "Nope.B2")
    with pytest.raises(UnoObjectError, match="No sheet named 'Missing Sheet'. Available: Sheet1, Data"):
        _parse_base_address(doc, "'Missing Sheet'!A1")

    pos = _parse_base_address(doc, "Data.B2")
    assert pos.Sheet == 1
    assert (pos.Column, pos.Row) == (1, 1)

    sheet4 = MagicMock()
    sheet4.getRangeAddress.return_value = MagicMock(Sheet=4)
    bare = _parse_base_address(doc, "C3", sheet_obj=sheet4)
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


def test_parse_flags_rejects_unknown_and_out_of_mask():
    for bad in ("printarea", ["printarea"], 16, -1, "16", 4 | 8 | 16):
        with pytest.raises(ValueError):
            _parse_flags(bad)
    assert _parse_flags(0) == 0
    assert _parse_flags("1,4,8") == 13


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
    with pytest.raises(UnoObjectError, match="No sheet named 'NonExistentSheet'"):
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
    res_info = _tool("named_range_get_info").execute_safe(ctx, name="NonExistent")
    assert res_info["status"] == "error"
    assert res_info["code"] == "NAMED_RANGE_NOT_FOUND"
    assert "named_range_list" in res_info["details"]["hint"]

    # 2. Add duplicate
    named_ranges.hasByName.return_value = True
    tool_add = _tool("named_range_add")
    res_add = tool_add.execute_safe(ctx, name="ExistingRange", content="A1")
    assert res_add["status"] == "error"
    assert res_add["code"] == "NAMED_RANGE_EXISTS"

    # 3. Edit not found
    named_ranges.hasByName.return_value = False
    tool_edit = _tool("named_range_edit")
    res_edit = tool_edit.execute_safe(ctx, name="NonExistent", content="B1")
    assert res_edit["status"] == "error"
    assert res_edit["code"] == "NAMED_RANGE_NOT_FOUND"

    # 4. Edit rename collision
    named_ranges.hasByName.side_effect = lambda name: name in ("RangeA", "RangeB")
    res_edit_coll = tool_edit.execute_safe(ctx, name="RangeA", new_name="RangeB")
    assert res_edit_coll["status"] == "error"
    assert res_edit_coll["code"] == "NAMED_RANGE_EXISTS"

    # 5. Delete not found
    named_ranges.hasByName.side_effect = None
    named_ranges.hasByName.return_value = False
    res_del = _tool("named_range_delete").execute_safe(ctx, name="NonExistent")
    assert res_del["status"] == "error"
    assert res_del["code"] == "NAMED_RANGE_NOT_FOUND"

    # 6. CreateFromTitles invalid border (rejected by schema validator)
    tool_titles = _tool("named_range_create_from_titles")
    for bad_border in ("invalid_border", "TOP"):
        prep = prepare_tool_call(
            tool_titles,
            SimpleNamespace(doc_type="calc"),
            "named_range_create_from_titles",
            {"range": ["A1:B5"], "border": bad_border},
        )
        assert prep.error is not None
        assert prep.error["code"] == "VALIDATION_ERROR"

    # 7. CreateFromTitles missing range
    res_titles = tool_titles.execute_safe(ctx, range=[])
    assert res_titles["status"] == "error"
    assert res_titles["code"] == "INVALID_PARAM"

    # 8. Add invalid base_cell
    named_ranges.hasByName.side_effect = None
    named_ranges.hasByName.return_value = False
    res_add_invalid_base = tool_add.execute_safe(ctx, name="NewRange", content="A1", base_cell="invalid!")
    assert res_add_invalid_base["status"] == "error"
    assert res_add_invalid_base["code"] == "INVALID_BASE_CELL"


def test_named_range_create_from_titles_maps_border():
    from com.sun.star.sheet.Border import LEFT, TOP

    doc = MagicMock()
    named_ranges = MagicMock()
    doc.NamedRanges = named_ranges
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
    ctx = MagicMock()
    ctx.doc = doc

    tool = _tool("named_range_create_from_titles")
    res_left = tool.execute_safe(ctx, range=["A1:B5"], border="left")
    assert res_left["status"] == "ok"
    named_ranges.addNewFromTitles.assert_called_with(range_addr, LEFT)

    res_default = tool.execute_safe(ctx, range=["A1:B5"])
    assert res_default["status"] == "ok"
    named_ranges.addNewFromTitles.assert_called_with(range_addr, TOP)


def test_defined_name_matches_calc_is_name_valid():
    """ScRangeData::IsNameValid rejects cell refs, dots, leading digits, and spaces."""
    for illegal in ("A1", "a1", "AA10", "Q1", "R1C1", "RC", "C1", "R2", "Sales.2026", "1abc", "My Range", "", "A1:B2", "$A$1", "XFD1"):
        assert _defined_name_error(illegal), illegal
    assert "cell" in (_defined_name_error("A1") or "").lower()
    assert "underscore" in (_defined_name_error("Sales.2026") or "").lower()
    assert "underscore" in (_defined_name_error("1abc") or "").lower()
    for legal in ("TaxRate", "_Hidden", "Sales_2026", "Q1Sales", "DATA1", "R2D2", "Sales?", "Ångström", "XFE1"):
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
    doc.getUndoManager.return_value = FakeUndoManager()
    ctx = MagicMock()
    ctx.doc = doc
    return ctx


def test_edit_rejects_illegal_rename_before_mutation():
    """Illegal new names never reach setName, and earlier fields stay unchanged."""
    nr = _nr("TaxRate", "$Sheet1.$A$1")
    ctx = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr})
    tool = _tool("named_range_edit")
    res = tool.execute_safe(ctx, name="TaxRate", new_name="A1", content="$Sheet1.$B$1")
    assert res["status"] == "error"
    assert res["code"] == "INVALID_NAME"
    nr.setName.assert_not_called()
    nr.setContent.assert_not_called()

    res_space = tool.execute_safe(ctx, name="TaxRate", new_name="Sales 2026", content="$Sheet1.$B$1")
    assert res_space["code"] == "INVALID_NAME"
    nr.setContent.assert_not_called()

    res_base = tool.execute_safe(ctx, name="TaxRate", new_name="TaxRate2", base_cell="invalid!")
    assert res_base["code"] == "INVALID_BASE_CELL"
    nr.setName.assert_not_called()


def test_edit_renames_before_other_fields_and_undoes_on_failure():
    """setName runs first. A later setter failure undoes the group in one step."""
    nr = _nr("TaxRate", "$Sheet1.$A$1")
    ctx = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr})
    order: list[tuple[str, str]] = []
    nr.setName.side_effect = lambda new: order.append(("name", new))
    nr.setContent.side_effect = lambda content: order.append(("content", content))
    nr.setType.side_effect = lambda mask: order.append(("type", str(mask)))

    res = _tool("named_range_edit").execute_safe(ctx, name="TaxRate", new_name="TaxRate2", content="$Sheet1.$B$1", flags=["print_area"])
    assert res["status"] == "ok"
    assert [step[0] for step in order] == ["name", "content", "type"]
    assert order[0] == ("name", "TaxRate2")

    nr_fail = _nr("TaxRate", "$Sheet1.$A$1")
    ctx_fail = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr_fail})
    fake_fail = ctx_fail.doc.getUndoManager.return_value
    nr_fail.setContent.side_effect = RuntimeException("content rejected")
    res_fail = _tool("named_range_edit").execute_safe(ctx_fail, name="TaxRate", new_name="TaxRate2", content="$Sheet1.$B$1")
    assert res_fail["status"] == "error"
    assert res_fail["code"] == "TOOL_EXECUTION_ERROR"
    assert [call.args[0] for call in nr_fail.setName.call_args_list] == ["TaxRate2"]
    assert fake_fail.undone == 1


def test_edit_undoes_earlier_setters_when_a_later_setter_fails():
    nr = _nr("TaxRate", "$Sheet1.$A$1")
    ctx = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr})
    fake = ctx.doc.getUndoManager.return_value
    nr.setType.side_effect = RuntimeException("type rejected")
    res = _tool("named_range_edit").execute_safe(ctx, name="TaxRate", new_name="TaxRate2", content="$Sheet1.$B$1", flags=["print_area"])
    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert [call.args[0] for call in nr.setName.call_args_list] == ["TaxRate2"]
    assert [call.args[0] for call in nr.setContent.call_args_list] == ["$Sheet1.$B$1"]
    assert fake.undone == 1
    assert fake.entered == ["WriterAgent: Edit named range"]

    # setReferencePosition failure undoes content, type, and rename via the bracket
    nr_pos = _nr("TaxRate", "$Sheet1.$A$1")
    ctx_pos = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr_pos})
    fake_pos = ctx_pos.doc.getUndoManager.return_value
    nr_pos.setReferencePosition.side_effect = RuntimeException("pos rejected")
    res_pos = _tool("named_range_edit").execute_safe(
        ctx_pos,
        name="TaxRate",
        new_name="TaxRate2",
        content="$Sheet1.$B$1",
        flags=["print_area"],
        base_cell="A1",
    )
    assert res_pos["status"] == "error"
    assert res_pos["code"] == "TOOL_EXECUTION_ERROR"
    assert [call.args[0] for call in nr_pos.setName.call_args_list] == ["TaxRate2"]
    assert [call.args[0] for call in nr_pos.setContent.call_args_list] == ["$Sheet1.$B$1"]
    assert [call.args[0] for call in nr_pos.setType.call_args_list] == [2]
    assert fake_pos.undone == 1
    assert fake_pos.entered == ["WriterAgent: Edit named range"]


def test_unqualified_name_prefers_sheet_local_shadow():
    """A bare name on a sheet with a local same-spelled range targets the local one."""
    local = _nr("Total", "local")
    glob = _nr("Total", "global")
    sheet = _sheet("Sheet1", {"Total": local})
    ctx = _workbook("Sheet1", [sheet], {"Total": glob})

    info = _tool("named_range_get_info").execute_safe(ctx, name="Total")
    assert info["status"] == "ok"
    assert info["result"]["scope"] == "Sheet1"
    assert info["result"]["content"] == "local"

    forced = _tool("named_range_get_info").execute_safe(ctx, name="Total", scope="global")
    assert forced["result"]["scope"] == "global"
    assert forced["result"]["content"] == "global"

    edit = _tool("named_range_edit").execute_safe(ctx, name="Total", content="$Sheet1.$C$1")
    assert edit["status"] == "ok"
    local.setContent.assert_called_once_with("$Sheet1.$C$1")
    glob.setContent.assert_not_called()

    glob_edit = _tool("named_range_edit").execute_safe(ctx, name="Total", scope="global", content="$Sheet1.$D$1")
    assert glob_edit["status"] == "ok"
    glob.setContent.assert_called_once_with("$Sheet1.$D$1")

    deleted = _tool("named_range_delete").execute_safe(ctx, name="Total")
    sheet.NamedRanges.removeByName.assert_called_once_with("Total")
    ctx.doc.NamedRanges.removeByName.assert_not_called()
    assert deleted["status"] == "ok"

    deleted_global = _tool("named_range_delete").execute_safe(ctx, name="Total", scope="global")
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

    listed = _tool("named_range_list").execute_safe(ctx, scope="all")
    assert listed["status"] == "ok"
    found = {(item["scope"], item["name"]) for item in listed["result"]}
    assert found == {("global", "Global"), ("Sheet1", "Visible"), ("Sheet2", "Other")}

    missing = _tool("named_range_get_info").execute_safe(ctx, name="Secret")
    assert missing["status"] == "error"
    assert missing["code"] == "NAMED_RANGE_NOT_FOUND"

    explicit = _tool("named_range_get_info").execute_safe(ctx, name="Secret", scope="_hidden")
    assert explicit["status"] == "ok"
    assert explicit["result"]["scope"] == "_hidden"
    assert explicit["result"]["content"] == "s"

    fallback = _tool("named_range_get_info").execute_safe(ctx, name="Other")
    assert fallback["status"] == "ok"
    assert fallback["result"]["scope"] == "Sheet2"


def test_get_info_bare_sheet_qualified_name_resolves_sheet_scope():
    """NamedRangeGetInfo on 'Data.LocalName' with no scope returns the Data sheet's range."""
    local = _nr("LocalName", "$Data.$A$1")
    data_sheet = _sheet("Data", {"LocalName": local})
    sheet1 = _sheet("Sheet1", {})
    ctx = _workbook("Sheet1", [sheet1, data_sheet], {})

    info = _tool("named_range_get_info").execute_safe(ctx, name="Data.LocalName")
    assert info["status"] == "ok"
    assert info["result"]["scope"] == "Data"
    assert info["result"]["name"] == "LocalName"
    assert info["result"]["content"] == "$Data.$A$1"


def test_edit_and_delete_resolve_sheet_qualified_name():
    """Edit and delete resolve 'Data.LocalName' to the Data sheet and operate with bare name."""
    local = _nr("LocalName", "$Data.$A$1")
    data_sheet = _sheet("Data", {"LocalName": local})
    sheet1 = _sheet("Sheet1", {})
    ctx = _workbook("Sheet1", [sheet1, data_sheet], {})

    res_edit = _tool("named_range_edit").execute_safe(ctx, name="Data.LocalName", content="$Data.$B$1")
    assert res_edit["status"] == "ok"
    local.setContent.assert_called_once_with("$Data.$B$1")

    res_del = _tool("named_range_delete").execute_safe(ctx, name="Data.LocalName")
    assert res_del["status"] == "ok"
    data_sheet.NamedRanges.removeByName.assert_called_once_with("LocalName")


def test_add_validates_name_and_flags():
    """NamedRangeAdd validates name and flags before container lookup or mutation."""
    doc = MagicMock()
    named_ranges = MagicMock()
    doc.NamedRanges = named_ranges
    ctx = MagicMock()
    ctx.doc = doc

    tool = _tool("named_range_add")
    res_bad_name = tool.execute_safe(ctx, name="A1", content="$Sheet1.$A$1")
    assert res_bad_name["status"] == "error"
    assert res_bad_name["code"] == "INVALID_NAME"
    named_ranges.addNewByName.assert_not_called()

    res_bad_flag = tool.execute_safe(ctx, name="TaxRate", content="$Sheet1.$A$1", flags=["printarea"])
    assert res_bad_flag["status"] == "error"
    assert res_bad_flag["code"] == "INVALID_FLAGS"
    named_ranges.addNewByName.assert_not_called()


def test_edit_rejects_unknown_flags_before_mutation():
    """NamedRangeEdit validates flags before setName or setContent mutations."""
    nr = _nr("TaxRate", "$Sheet1.$A$1")
    ctx = _workbook("Sheet1", [_sheet("Sheet1", {})], {"TaxRate": nr})
    tool = _tool("named_range_edit")
    res = tool.execute_safe(ctx, name="TaxRate", new_name="TaxRate2", flags=["printarea"])
    assert res["status"] == "error"
    assert res["code"] == "INVALID_FLAGS"
    nr.setName.assert_not_called()
    nr.setContent.assert_not_called()


def test_named_range_tools_missing_sheet_is_uno_object_error():
    ctx = _workbook("Sheet1", [_sheet("Sheet1", {})], {})
    ctx.doc.getSheets.return_value.getElementNames.return_value = ("Sheet1",)

    res_info = _tool("named_range_get_info").execute_safe(ctx, name="X", scope="Nope")
    assert res_info["status"] == "error"
    assert res_info["code"] == "UNO_OBJECT_ERROR"
    assert "Available: Sheet1" in res_info["message"]
    assert "sheet_list" in res_info["details"]["hint"]

    res_add = _tool("named_range_add").execute_safe(ctx, name="X", content="A1", base_cell="Nope.B2")
    assert res_add["status"] == "error"
    assert res_add["code"] == "UNO_OBJECT_ERROR"
    assert "Available: Sheet1" in res_add["message"]
    assert "sheet_list" in res_add["details"]["hint"]
    ctx.doc.NamedRanges.addNewByName.assert_not_called()


def test_named_range_create_from_titles_two_ranges_is_invalid_param():
    ctx = _workbook("Sheet1", [_sheet("Sheet1", {})], {})
    res = _tool("named_range_create_from_titles").execute_safe(ctx, range=["A1:B2", "C1:D2"])
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    ctx.doc.NamedRanges.addNewFromTitles.assert_not_called()
