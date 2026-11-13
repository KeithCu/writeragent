# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc DataPilot (pivot table) tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from plugin.calc.pivot import AVERAGE, CreatePivotTable, DeletePivotTable, ListPivotTables, RefreshPivotTable
from plugin.framework.errors import UnoObjectError, name_miss_details
from plugin.framework.tool import ToolContext, ToolRegistry
from plugin.tests.testing_utils import FakeUndoManager


class DisposedException(Exception):
    pass


def _run(doc, tool_name: str, **kw):
    registry = ToolRegistry(MagicMock())
    for cls in (CreatePivotTable, RefreshPivotTable, DeletePivotTable, ListPivotTables):
        registry.register(cls())
    ctx = ToolContext(doc=doc, ctx=MagicMock(), doc_type="calc", services=MagicMock(), caller="test")
    return registry.execute(tool_name, ctx, **kw)


def _dp_tables(pivots: dict):
    dp = MagicMock()
    dp.hasByName.side_effect = lambda name: name in pivots
    dp.getByName.side_effect = lambda name: pivots[name]
    dp.getElementNames.side_effect = lambda: tuple(pivots.keys())
    dp.removeByName.side_effect = lambda name: pivots.pop(name)
    return dp


def _sheet(name: str, pivots: dict | None = None, dp=None):
    sh = MagicMock()
    sh.getName.return_value = name
    dp_obj = dp if dp is not None else _dp_tables(pivots if pivots is not None else {})
    sh.queryInterface.return_value.getDataPilotTables.return_value = dp_obj
    return sh


def _doc(*sheets):
    by_name = {s.getName(): s for s in sheets}
    names = tuple(by_name.keys())
    coll = MagicMock()
    coll.getElementNames.side_effect = lambda: names
    coll.hasByName.side_effect = lambda n: n in by_name
    coll.getByName.side_effect = lambda n: by_name[n]
    coll.getByIndex.side_effect = lambda i: sheets[i]
    doc = MagicMock()
    doc.getSheets.return_value = coll
    return doc


def _make_mock_cell(val: str):
    c = MagicMock()
    c.getString.return_value = val
    return c


def test_pivot_table_list_all_sheets():
    ora = SimpleNamespace(Sheet=0, StartColumn=3, StartRow=0, EndColumn=6, EndRow=10)
    dpt = MagicMock()
    dpt.getOutputRange.return_value = ora
    tbl_any = MagicMock()
    tbl_any.queryInterface.return_value = dpt

    doc = _doc(_sheet("Sheet1", {"Pivot1": tbl_any}))
    res = _run(doc, "pivot_table_list")

    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["pivot_tables"] == [
        {
            "name": "Pivot1",
            "sheet": "Sheet1",
            "output_sheet_index": 0,
            "output_range": "D1:G11",
        }
    ]


def test_pivot_table_list_skips_internal_sheets():
    tbl_hidden = MagicMock()
    tbl_hidden.queryInterface.return_value = None
    tbl_visible = MagicMock()
    tbl_visible.queryInterface.return_value = None

    doc = _doc(_sheet("_wa_probe", {"Hidden": tbl_hidden}), _sheet("Sheet1", {"Pivot1": tbl_visible}))
    res = _run(doc, "pivot_table_list")

    assert res["status"] == "ok"
    assert [p["name"] for p in res["pivot_tables"]] == ["Pivot1"]


def test_pivot_table_list_sheet_not_found():
    doc = _doc(_sheet("Sheet1"), _sheet("Data"))
    res = _run(doc, "pivot_table_list", sheet="NoSuchSheet")

    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "No sheet named 'NoSuchSheet'." in res["message"]
    assert "Available: Sheet1, Data" in res["message"]
    assert res["details"]["hint"] == "Call sheet_list to list names."


def test_pivot_table_create_function_mapping_and_page_filters():
    src_range = MagicMock()
    src_addr = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=2, EndRow=5)
    src_range.getRangeAddress.return_value = src_addr
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales"), _make_mock_cell("Year")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_cell = MagicMock()
    dest_cell.getRangeAddress.return_value = SimpleNamespace(Sheet=1, StartColumn=0, StartRow=0, EndColumn=0, EndRow=0)

    dp_tables = MagicMock()
    inserted = False

    def mock_insert(name, addr, desc):
        nonlocal inserted
        inserted = True

    dp_tables.insertNewByName.side_effect = mock_insert
    dp_tables.hasByName.side_effect = lambda name: inserted and name == "MyPivot"
    dp_tables.getElementNames.side_effect = [(), ("MyPivot",)]

    desc = MagicMock()
    dp_tables.createDataPilotDescriptor.return_value = desc
    filter_desc = MagicMock()
    desc.getFilterDescriptor.return_value = filter_desc

    f_region = MagicMock()
    f_region.getName.return_value = "Region"
    f_sales = MagicMock()
    f_sales.getName.return_value = "Sales"
    f_year = MagicMock()
    f_year.getName.return_value = "Year"

    fields = MagicMock()
    fields.getCount.return_value = 3
    fields.getByIndex.side_effect = lambda i: [f_region, f_sales, f_year][i]
    desc.getDataPilotFields.return_value = fields

    doc = _doc(_sheet("Sheet1"), _sheet("Sheet2", dp=dp_tables))

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:C6" in ref else dest_cell

        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="Sheet1.A1:C6",
            destination_cell="Sheet2.A1",
            row_fields=["Region"],
            data_fields=["Sales"],
            page_fields=["Year"],
            data_functions={"Sales": "AVERAGE"},
            page_filters={"Year": 2024},
        )

    assert res["status"] == "ok"
    f_sales.setPropertyValue.assert_any_call("Function", AVERAGE)
    assert filter_desc.setFilterFields.called
    fields_tuple = filter_desc.setFilterFields.call_args[0][0]
    assert len(fields_tuple) == 1
    assert fields_tuple[0].Field == 2
    assert fields_tuple[0].NumericValue == 2024.0


def test_pivot_table_create_unknown_function_error():
    src_range = MagicMock()
    src_addr = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    src_range.getRangeAddress.return_value = src_addr
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_cell = MagicMock()
    dest_cell.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=5, StartRow=0, EndColumn=5, EndRow=0)

    dp_tables = MagicMock()
    dp_tables.hasByName.return_value = False
    desc = MagicMock()
    dp_tables.createDataPilotDescriptor.return_value = desc

    f_region = MagicMock()
    f_region.getName.return_value = "Region"
    f_sales = MagicMock()
    f_sales.getName.return_value = "Sales"
    fields = MagicMock()
    fields.getCount.return_value = 2
    fields.getByIndex.side_effect = lambda i: [f_region, f_sales][i]
    desc.getDataPilotFields.return_value = fields

    doc = _doc(_sheet("Sheet1", dp=dp_tables))

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_cell

        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
            data_functions={"Sales": "NONEXISTENT"},
        )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert "Unknown data function 'NONEXISTENT'" in res["message"]


def test_pivot_table_create_header_and_row_validation():
    doc = _doc(_sheet("Sheet1"))

    # Single-row source
    src_range_1row = MagicMock()
    src_range_1row.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=0)

    dest_cell = MagicMock()
    dest_cell.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=5, StartRow=0, EndColumn=5, EndRow=0)

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range_1row if "A1:B1" in ref else dest_cell

        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B1",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert "at least 2 rows" in res["message"]

    # Empty header cell
    src_range_empty_hdr = MagicMock()
    src_range_empty_hdr.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    cells = [_make_mock_cell("Region"), _make_mock_cell("")]
    src_range_empty_hdr.getCellByPosition.side_effect = lambda col, row: cells[col]

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range_empty_hdr if "A1:B4" in ref else dest_cell

        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert "Header cell in column 2 of source range is empty" in res["message"]


def test_pivot_table_create_undoes_insert_on_rename_or_refresh_failure():
    src_range = MagicMock()
    src_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_cell = MagicMock()
    dest_cell.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=5, StartRow=0, EndColumn=5, EndRow=0)

    dp_tables = MagicMock()
    dp_tables.hasByName.return_value = False

    desc = MagicMock()
    dp_tables.createDataPilotDescriptor.return_value = desc
    f_sales = MagicMock()
    f_sales.getName.return_value = "Sales"
    fields = MagicMock()
    fields.getCount.return_value = 1
    fields.getByIndex.return_value = f_sales
    desc.getDataPilotFields.return_value = fields

    dest_sheet = _sheet("Sheet1", dp=dp_tables)
    doc = _doc(dest_sheet)
    doc.getSheets().getElementNames.side_effect = lambda: ()
    fake_undo = FakeUndoManager()
    doc.getUndoManager.return_value = fake_undo

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_cell

        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert "was not created" in res["message"]
    dp_tables.removeByName.assert_not_called()
    assert fake_undo.undone == 1

    # Refresh failure undoes the insert instead of calling removeByName
    dpt = MagicMock()
    dpt.refresh.side_effect = RuntimeError("refresh boom")
    tbl = MagicMock()
    tbl.queryInterface.return_value = dpt
    dp_refresh = MagicMock()
    dp_refresh.hasByName.return_value = True
    dp_refresh.getByName.return_value = tbl
    dp_refresh.createDataPilotDescriptor.return_value = desc
    dest_sheet2 = _sheet("Sheet1", dp=dp_refresh)
    doc2 = _doc(dest_sheet2)
    doc2.getSheets().getElementNames.side_effect = lambda: ()
    fake_undo2 = FakeUndoManager()
    doc2.getUndoManager.return_value = fake_undo2

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_cell

        res2 = _run(
            doc2,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res2["status"] == "error"
    assert "Failed to refresh new pivot table" in res2["message"]
    dp_refresh.removeByName.assert_not_called()
    assert fake_undo2.undone == 1


def test_pivot_table_refresh_and_delete():
    dpt = MagicMock()
    table_mock = MagicMock()
    table_mock.queryInterface.return_value = dpt
    pivots = {"MyPivot": table_mock}
    doc = _doc(_sheet("Sheet1"), _sheet("Sheet2", pivots))

    res_ref = _run(doc, "pivot_table_refresh", name="MyPivot")
    assert res_ref["status"] == "ok"
    dpt.refresh.assert_called_once()

    res_del = _run(doc, "pivot_table_delete", name="MyPivot")
    assert res_del["status"] == "ok"
    assert res_del["message"] == "Deleted pivot table 'MyPivot' from sheet 'Sheet2'."
    assert "MyPivot" not in pivots


def test_pivot_table_create_page_filter_not_in_page_fields():
    src_range = MagicMock()
    src_addr = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    src_range.getRangeAddress.return_value = src_addr
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_cell = MagicMock()
    dest_cell.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=5, StartRow=0, EndColumn=5, EndRow=0)

    dp_tables = MagicMock()
    dp_tables.hasByName.return_value = False
    desc = MagicMock()
    dp_tables.createDataPilotDescriptor.return_value = desc

    f_region = MagicMock()
    f_region.getName.return_value = "Region"
    f_sales = MagicMock()
    f_sales.getName.return_value = "Sales"
    fields = MagicMock()
    fields.getCount.return_value = 2
    fields.getByIndex.side_effect = lambda i: [f_region, f_sales][i]
    desc.getDataPilotFields.return_value = fields

    doc = _doc(_sheet("Sheet1", dp=dp_tables))

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_cell

        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
            page_filters={"Region": "North"},
        )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert "Field 'Region' in page_filters must be in page_fields." in res["message"]


def test_resolve_qualified_ref_quotes_apostrophe_sheet():
    from plugin.calc.pivot import _resolve_qualified_ref

    bridge = MagicMock()
    _resolve_qualified_ref(bridge, "A1:B3", "O'Brien", "source_range", "source_sheet")
    bridge.resolve_range_or_address.assert_called_with("'O''Brien'.A1:B3")

    bridge.reset_mock()
    _resolve_qualified_ref(bridge, "A1:B3", "Q1-2026", "source_range", "source_sheet")
    bridge.resolve_range_or_address.assert_called_with("Q1-2026.A1:B3")


@pytest.mark.parametrize("tool_name", ["pivot_table_refresh", "pivot_table_delete"])
@pytest.mark.parametrize("sheet_arg", [None, "Sales"])
def test_pivot_miss_is_pivot_table_not_found(tool_name, sheet_arg):
    doc = _doc(_sheet("Sales", {"Pivot1": MagicMock()}), _sheet("_wa", {"Pivot2": MagicMock()}))
    kw = {"name": "Pivot3"}
    if sheet_arg is not None:
        kw["sheet"] = sheet_arg
    res = _run(doc, tool_name, **kw)

    expected_msg = (
        f"Pivot table 'Pivot3' not found on sheet '{sheet_arg}'."
        if sheet_arg is not None
        else "Pivot table 'Pivot3' not found."
    )
    assert res["status"] == "error"
    assert res["code"] == "PIVOT_TABLE_NOT_FOUND"
    assert res["message"] == expected_msg
    assert res["details"]["hint"] == "Call pivot_table_list to list names."
    assert res["details"]["similar"] == ["Pivot1"]


def test_pivot_bare_name_skips_internal_sheets():
    dpt = MagicMock()
    tbl = MagicMock()
    tbl.queryInterface.return_value = dpt
    doc = _doc(_sheet("Sheet1"), _sheet("_wa", {"Secret": tbl}))

    res_bare = _run(doc, "pivot_table_refresh", name="Secret")
    assert res_bare["status"] == "error"
    assert res_bare["code"] == "PIVOT_TABLE_NOT_FOUND"

    res_explicit = _run(doc, "pivot_table_refresh", name="Secret", sheet="_wa")
    assert res_explicit["status"] == "ok"
    dpt.refresh.assert_called_once()


@pytest.mark.parametrize("tool_name", ["pivot_table_refresh", "pivot_table_delete"])
def test_pivot_missing_sheet_is_bridge_error(tool_name):
    doc = _doc(_sheet("Sheet1"), _sheet("_wa"))
    res = _run(doc, tool_name, name="Pivot1", sheet="Sheet9")

    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert res["message"] == "No sheet named 'Sheet9'. Available: Sheet1"
    assert res["details"]["hint"] == "Call sheet_list to list names."
    assert res["details"]["similar"] == ["Sheet1"]


def test_pivot_sheet_lookup_disposal_is_document_disposed():
    doc = _doc(_sheet("Sheet1"))
    doc.getSheets().hasByName.side_effect = DisposedException("gone")

    res = _run(doc, "pivot_table_refresh", name="Pivot1", sheet="Sheet1")
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_pivot_create_missing_sheet_is_bridge_error():
    doc = _doc(_sheet("Sheet1"))
    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge_cls.return_value.resolve_range_or_address.side_effect = UnoObjectError(
            "No sheet named 'Nope'. Available: Sheet1",
            details=name_miss_details("Nope", ["Sheet1"], "sheet_list"),
        )
        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            source_sheet="Nope",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert res["message"] == "No sheet named 'Nope'. Available: Sheet1"
    assert res["details"]["hint"] == "Call sheet_list to list names."


def test_pivot_create_unresolvable_ref_names_argument():
    doc = _doc(_sheet("Sheet1"))
    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge_cls.return_value.resolve_range_or_address.side_effect = RuntimeError("bad ref")
        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="NotAName",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["message"] == "Cannot resolve source_range 'NotAName': bad ref"


def test_pivot_create_resolve_disposal_is_document_disposed():
    doc = _doc(_sheet("Sheet1"))
    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge_cls.return_value.resolve_range_or_address.side_effect = DisposedException("gone")
        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_pivot_create_header_read_disposal_is_document_disposed():
    src_range = MagicMock()
    src_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    src_range.getCellByPosition.side_effect = DisposedException("gone")
    doc = _doc(_sheet("Sheet1"))

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge_cls.return_value.resolve_range_or_address.return_value = src_range
        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_pivot_create_range_destination_anchors_top_left():
    src_range = MagicMock()
    src_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_obj = MagicMock(spec=["getRangeAddress"])
    dest_obj.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=5, StartRow=2, EndColumn=6, EndRow=3)

    dp_tables = MagicMock()
    inserted = False

    def mock_insert(name, addr, desc):
        nonlocal inserted
        inserted = True

    dp_tables.insertNewByName.side_effect = mock_insert
    dp_tables.hasByName.side_effect = lambda name: inserted and name == "MyPivot"
    dp_tables.getElementNames.side_effect = [(), ("MyPivot",)]

    desc = MagicMock()
    dp_tables.createDataPilotDescriptor.return_value = desc
    f_sales = MagicMock()
    f_sales.getName.return_value = "Sales"
    fields = MagicMock()
    fields.getCount.return_value = 1
    fields.getByIndex.return_value = f_sales
    desc.getDataPilotFields.return_value = fields

    doc = _doc(_sheet("Sheet1", dp=dp_tables))

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_obj

        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F3:G4",
            data_fields=["Sales"],
        )

    assert res["status"] == "ok"
    dest_addr = dp_tables.insertNewByName.call_args[0][1]
    assert dest_addr.Sheet == 0
    assert dest_addr.Column == 5
    assert dest_addr.Row == 2


def test_pivot_create_rejects_name_used_on_internal_sheet():
    src_range = MagicMock()
    src_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_cell = MagicMock()
    dest_cell.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=5, StartRow=0, EndColumn=5, EndRow=0)

    dp_tables_visible = MagicMock()
    dp_tables_visible.hasByName.return_value = False
    doc = _doc(_sheet("Sheet1", dp=dp_tables_visible), _sheet("_wa", {"MyPivot": MagicMock()}))

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_cell

        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["message"] == "Pivot table 'MyPivot' already exists on sheet '_wa'."
    dp_tables_visible.insertNewByName.assert_not_called()


def test_pivot_table_create_duplicate_field_is_invalid_param():
    src_range = MagicMock()
    src_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_cell = MagicMock()
    dest_cell.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=5, StartRow=0, EndColumn=5, EndRow=0)

    dp_tables = MagicMock()
    dp_tables.hasByName.return_value = False
    sheet = _sheet("Sheet1", dp=dp_tables)
    doc = _doc(sheet)

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_cell
        res = _run(
            doc,
            "pivot_table_create",
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            row_fields=["Sales"],
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert "Duplicate field name" in res["message"]
    dp_tables.insertNewByName.assert_not_called()
