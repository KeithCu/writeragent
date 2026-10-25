# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc DataPilot (pivot table) tools."""

from __future__ import annotations

import uno
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from plugin.calc.pivot import AVERAGE, SUM, CreatePivotTable, DeletePivotTable, ListPivotTables, RefreshPivotTable
from plugin.framework.errors import UnoObjectError


def test_list_pivot_tables_all_sheets():
    doc = MagicMock()
    sheets = MagicMock()
    doc.getSheets.return_value = sheets
    sheets.getElementNames.return_value = ("Sheet1",)

    sheet1 = MagicMock()
    sheet1.getName.return_value = "Sheet1"
    sheets.getByName.return_value = sheet1

    dp_tables = MagicMock()
    dp_tables.getElementNames.return_value = ("Pivot1",)
    tbl_any = MagicMock()
    dp_tables.getByName.return_value = tbl_any

    ora = SimpleNamespace(Sheet=0, StartColumn=3, StartRow=0, EndColumn=6, EndRow=10)
    dpt = MagicMock()
    dpt.getOutputRange.return_value = ora

    ctx = MagicMock()
    ctx.doc = doc

    with patch("plugin.calc.pivot._get_dp_tables", return_value=dp_tables), \
         patch("plugin.calc.pivot._query_interface", return_value=dpt):
        tool = ListPivotTables()
        res = tool.execute(ctx)

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


def test_list_pivot_tables_skips_internal_sheets():
    doc = MagicMock()
    sheets = MagicMock()
    doc.getSheets.return_value = sheets
    sheets.getElementNames.return_value = ("_wa_probe", "Sheet1")

    sheet1 = MagicMock()
    sheet1.getName.return_value = "Sheet1"
    sheets.getByName.return_value = sheet1

    dp_tables = MagicMock()
    dp_tables.getElementNames.return_value = ()

    ctx = MagicMock()
    ctx.doc = doc

    with patch("plugin.calc.pivot._get_dp_tables", return_value=dp_tables):
        tool = ListPivotTables()
        res = tool.execute(ctx)

    assert res["status"] == "ok"
    # Should not call getByName on _wa_probe
    sheets.getByName.assert_called_once_with("Sheet1")


def test_list_pivot_tables_sheet_not_found():
    doc = MagicMock()
    sheets = MagicMock()
    doc.getSheets.return_value = sheets
    sheets.hasByName.return_value = False
    sheets.getElementNames.return_value = ("Sheet1", "Data")

    ctx = MagicMock()
    ctx.doc = doc

    tool = ListPivotTables()
    with pytest.raises(UnoObjectError) as exc_info:
        tool.execute(ctx, sheet="NoSuchSheet")
    assert "No sheet named 'NoSuchSheet'." in str(exc_info.value)
    assert "Available: Sheet1, Data" in str(exc_info.value)

    res = tool.execute_safe(ctx, sheet="NoSuchSheet")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "No sheet named 'NoSuchSheet'." in res["message"]
    assert "Available: Sheet1, Data" in res["message"]


def _make_mock_cell(val: str):
    c = MagicMock()
    c.getString.return_value = val
    c.String = val
    return c


def test_create_pivot_table_function_mapping_and_page_filters():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    # Setup source range
    src_range = MagicMock()
    src_addr = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=2, EndRow=5)
    src_range.getRangeAddress.return_value = src_addr
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales"), _make_mock_cell("Year")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    # Setup dest cell
    dest_cell = MagicMock()
    dest_cell.getCellAddress.return_value = SimpleNamespace(Sheet=1, Column=0, Row=0)

    # Setup sheets
    dest_sheet = MagicMock()
    dest_sheet.getName.return_value = "Sheet2"
    doc.getSheets().getByIndex.return_value = dest_sheet
    doc.getSheets().getElementNames.return_value = ("Sheet1", "Sheet2")
    doc.getSheets().getByName.return_value = dest_sheet

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

    # Setup fields
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

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls, \
         patch("plugin.calc.pivot._get_dp_tables", return_value=dp_tables), \
         patch("plugin.calc.pivot._query_interface") as qi_mock:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:C6" in ref else dest_cell
        dpt = MagicMock()
        qi_mock.return_value = dpt

        tool = CreatePivotTable()
        res = tool.execute(
            ctx,
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


def test_create_pivot_table_unknown_function_error():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    src_range = MagicMock()
    src_addr = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    src_range.getRangeAddress.return_value = src_addr
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_cell = MagicMock()
    dest_cell.getCellAddress.return_value = SimpleNamespace(Sheet=0, Column=5, Row=0)

    dest_sheet = MagicMock()
    dest_sheet.getName.return_value = "Sheet1"
    doc.getSheets().getByIndex.return_value = dest_sheet

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

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls, \
         patch("plugin.calc.pivot._get_dp_tables", return_value=dp_tables):
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_cell

        tool = CreatePivotTable()
        res = tool.execute(
            ctx,
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
            data_functions={"Sales": "NONEXISTENT"},
        )

    assert res["status"] == "error"
    assert "Unknown data function 'NONEXISTENT'" in res["message"]


def test_create_pivot_table_header_and_row_validation():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    # Single-row source
    src_range_1row = MagicMock()
    src_range_1row.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=0)

    dest_cell = MagicMock()
    dest_cell.getCellAddress.return_value = SimpleNamespace(Sheet=0, Column=5, Row=0)

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range_1row if "A1:B1" in ref else dest_cell

        tool = CreatePivotTable()
        res = tool.execute(
            ctx,
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

        tool = CreatePivotTable()
        res = tool.execute(
            ctx,
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    assert "Header cell in column 2 of source range is empty" in res["message"]


def test_create_pivot_table_cleanup_on_rename_or_refresh_failure():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    src_range = MagicMock()
    src_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=3)
    cells = [_make_mock_cell("Region"), _make_mock_cell("Sales")]
    src_range.getCellByPosition.side_effect = lambda col, row: cells[col]

    dest_cell = MagicMock()
    dest_cell.getCellAddress.return_value = SimpleNamespace(Sheet=0, Column=5, Row=0)

    dest_sheet = MagicMock()
    dest_sheet.getName.return_value = "Sheet1"
    doc.getSheets().getByIndex.return_value = dest_sheet

    dp_tables = MagicMock()
    # Before insert: (), after insert: ("MyPivot_2",) -> collision auto-rename
    dp_tables.getElementNames.side_effect = [(), ("MyPivot_2",)]
    dp_tables.hasByName.return_value = False

    desc = MagicMock()
    dp_tables.createDataPilotDescriptor.return_value = desc
    f_sales = MagicMock()
    f_sales.getName.return_value = "Sales"
    fields = MagicMock()
    fields.getCount.return_value = 1
    fields.getByIndex.return_value = f_sales
    desc.getDataPilotFields.return_value = fields

    with patch("plugin.calc.pivot.CalcBridge") as bridge_cls, \
         patch("plugin.calc.pivot._get_dp_tables", return_value=dp_tables), \
         patch("plugin.calc.pivot._find_pivot", return_value="not found"):
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.side_effect = lambda ref: src_range if "A1:B4" in ref else dest_cell

        tool = CreatePivotTable()
        res = tool.execute(
            ctx,
            name="MyPivot",
            source_range="A1:B4",
            destination_cell="F1",
            data_fields=["Sales"],
        )

    assert res["status"] == "error"
    dp_tables.removeByName.assert_called_once_with("MyPivot_2")


def test_refresh_and_delete_pivot_table():
    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    table_mock = MagicMock()
    sheet_mock = MagicMock()
    sheet_mock.getName.return_value = "Sheet1"

    # Refresh success
    with patch("plugin.calc.pivot._find_pivot", return_value=(table_mock, sheet_mock)), \
         patch("plugin.calc.pivot._query_interface") as qi_mock:
        dpt = MagicMock()
        qi_mock.return_value = dpt

        refresh_tool = RefreshPivotTable()
        res = refresh_tool.execute(ctx, name="MyPivot")
        assert res["status"] == "ok"
        dpt.refresh.assert_called_once()

    # Refresh not found
    with patch("plugin.calc.pivot._find_pivot", return_value="Pivot table 'MyPivot' not found."):
        refresh_tool = RefreshPivotTable()
        res = refresh_tool.execute(ctx, name="MyPivot")
        assert res["status"] == "error"
        assert "not found" in res["message"]

    # Delete success
    dp_tables = MagicMock()
    with patch("plugin.calc.pivot._find_pivot", return_value=(table_mock, sheet_mock)), \
         patch("plugin.calc.pivot._get_dp_tables", return_value=dp_tables):
        del_tool = DeletePivotTable()
        res = del_tool.execute(ctx, name="MyPivot")
        assert res["status"] == "ok"
        dp_tables.removeByName.assert_called_once_with("MyPivot")

    # Delete not found
    with patch("plugin.calc.pivot._find_pivot", return_value="Pivot table 'NoSuchPivot' not found."):
        del_tool = DeletePivotTable()
        res = del_tool.execute(ctx, name="NoSuchPivot")
        assert res["status"] == "error"
        assert "not found" in res["message"]
