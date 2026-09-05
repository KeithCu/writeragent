# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for DuckDB sibling used-range ingress and table source identity."""

from __future__ import annotations

import os
import tempfile

import uno

from plugin.calc.duckdb_tools import (
    QueryFolderSqlTool,
    _read_sibling_office_file_as_grid,
    parse_table_source_spec,
    read_model_table_grid,
)
from plugin.framework.errors import ToolExecutionError
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.format import create_property_value


def _fill_offset_table(doc) -> None:
    """Write a 2x2 table at C5 so A1:AK2000 would include huge empty padding."""
    sheets = doc.getSheets()
    if sheets.hasByName("Actuals"):
        sheet = sheets.getByName("Actuals")
    else:
        sheets.insertNewByName("Actuals", 0)
        sheet = sheets.getByName("Actuals")
    sheet.getCellByPosition(2, 4).setString("Region")
    sheet.getCellByPosition(3, 4).setString("Sales")
    sheet.getCellByPosition(2, 5).setString("North")
    sheet.getCellByPosition(3, 5).setValue(100)


def _store_sibling(doc, path: str, *, xlsx: bool = False) -> None:
    url = uno.systemPathToFileUrl(path)
    if xlsx:
        props = (create_property_value("FilterName", "Calc MS Excel 2007 XML"),)
        doc.storeToURL(url, props)
    else:
        doc.storeAsURL(url, ())


def _cleanup_dir(temp_dir: str) -> None:
    if not temp_dir or not os.path.isdir(temp_dir):
        return
    for name in os.listdir(temp_dir):
        try:
            os.remove(os.path.join(temp_dir, name))
        except OSError:
            pass
    try:
        os.rmdir(temp_dir)
    except OSError:
        pass


@native_test
@with_native_doc("calc")
def test_sibling_ods_used_range_is_tight(ctx, doc):
    temp_dir = tempfile.mkdtemp(prefix="wa_duckdb_sib_")
    sibling = TestingFactory.create_native_doc(ctx, "calc", hidden=True)
    try:
        _fill_offset_table(sibling)
        ods_path = os.path.join(temp_dir, "budget.ods")
        _store_sibling(sibling, ods_path)
        TestingFactory.close_doc(sibling)
        sibling = None

        _tbl, grid = _read_sibling_office_file_as_grid(ctx, ods_path, sheet_hint="Actuals")
        assert len(grid) == 2, f"used-range padded rows: {len(grid)}"
        assert len(grid[0]) == 2, f"used-range padded cols: {len(grid[0])}"
        assert grid[0][0] == "Region"
        assert grid[0][1] == "Sales"
        assert grid[1][0] == "North"
        assert grid[1][1] == 100
    finally:
        if sibling is not None:
            TestingFactory.close_doc(sibling)
        _cleanup_dir(temp_dir)


@native_test
@with_native_doc("calc")
def test_sibling_xlsx_used_range_is_tight(ctx, doc):
    temp_dir = tempfile.mkdtemp(prefix="wa_duckdb_sibx_")
    sibling = TestingFactory.create_native_doc(ctx, "calc", hidden=True)
    try:
        _fill_offset_table(sibling)
        xlsx_path = os.path.join(temp_dir, "budget.xlsx")
        _store_sibling(sibling, xlsx_path, xlsx=True)
        TestingFactory.close_doc(sibling)
        sibling = None

        _tbl, grid = _read_sibling_office_file_as_grid(ctx, xlsx_path, sheet_hint="Actuals")
        assert len(grid) == 2, f"used-range padded rows: {len(grid)}"
        assert len(grid[0]) == 2, f"used-range padded cols: {len(grid[0])}"
        assert grid[0] == ["Region", "Sales"] or grid[0][0] == "Region"
        assert grid[1][0] == "North"
    finally:
        if sibling is not None:
            TestingFactory.close_doc(sibling)
        _cleanup_dir(temp_dir)


@native_test
@with_native_doc("calc")
def test_sibling_bad_sheet_hint_errors(ctx, doc):
    temp_dir = tempfile.mkdtemp(prefix="wa_duckdb_hint_")
    sibling = TestingFactory.create_native_doc(ctx, "calc", hidden=True)
    try:
        _fill_offset_table(sibling)
        ods_path = os.path.join(temp_dir, "budget.ods")
        _store_sibling(sibling, ods_path)
        TestingFactory.close_doc(sibling)
        sibling = None

        try:
            _read_sibling_office_file_as_grid(ctx, ods_path, sheet_hint="MissingSheet")
        except ToolExecutionError as exc:
            msg = str(exc)
            assert "budget.ods" in msg
            assert "MissingSheet" in msg
        else:
            raise AssertionError("missing #SheetName hint must fail loud")
    finally:
        if sibling is not None:
            TestingFactory.close_doc(sibling)
        _cleanup_dir(temp_dir)


@native_test
@with_native_doc("calc")
def test_sibling_empty_sheet_errors(ctx, doc):
    temp_dir = tempfile.mkdtemp(prefix="wa_duckdb_empty_")
    sibling = TestingFactory.create_native_doc(ctx, "calc", hidden=True)
    try:
        ods_path = os.path.join(temp_dir, "empty.ods")
        _store_sibling(sibling, ods_path)
        TestingFactory.close_doc(sibling)
        sibling = None

        try:
            _read_sibling_office_file_as_grid(ctx, ods_path)
        except ToolExecutionError as exc:
            msg = str(exc).lower()
            assert "empty.ods" in str(exc)
            assert "empty" in msg
        else:
            raise AssertionError("empty used-range must fail loud")
    finally:
        if sibling is not None:
            TestingFactory.close_doc(sibling)
        _cleanup_dir(temp_dir)


def _sales_on_sheet(sheet) -> None:
    sheet.getCellByPosition(2, 4).setString("Region")
    sheet.getCellByPosition(3, 4).setString("Sales")
    sheet.getCellByPosition(2, 5).setString("North")
    sheet.getCellByPosition(3, 5).setValue(100)


def _append_south_row(sheet) -> None:
    sheet.getCellByPosition(2, 6).setString("South")
    sheet.getCellByPosition(3, 6).setValue(50)


@native_test
@with_native_doc("calc")
def test_sheet_identity_used_range_grows(ctx, doc):
    """{sheet} is stable identity: append a row and the same spec reads the new used range."""
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = sheet.getName()
    _sales_on_sheet(sheet)
    parsed = parse_table_source_spec({"sheet": sheet_name})

    first = read_model_table_grid(doc, parsed)
    assert len(first) == 2
    assert first[0] == ["Region", "Sales"]
    assert parsed.get("range") is None

    _append_south_row(sheet)
    second = read_model_table_grid(doc, parsed)
    assert len(second) == 3
    assert second[2][0] == "South"
    assert parsed["sheet"] == sheet_name
    assert parsed.get("range") is None


@native_test
@with_native_doc("calc")
def test_named_range_identity_follows_current_bounds(ctx, doc):
    """{named_range} resolves referred cells at read time, not a stored A1."""
    from com.sun.star.table import CellAddress

    sheet = doc.getSheets().getByIndex(0)
    sheet_name = sheet.getName()
    _sales_on_sheet(sheet)
    if doc.NamedRanges.hasByName("SalesData"):
        doc.NamedRanges.removeByName("SalesData")
    doc.NamedRanges.addNewByName(
        "SalesData",
        f"${sheet_name}.$C$5:$D$6",
        CellAddress(Sheet=0, Column=2, Row=4),
        0,
    )
    parsed = parse_table_source_spec({"named_range": "SalesData"})

    first = read_model_table_grid(doc, parsed)
    assert len(first) == 2
    assert first[1][0] == "North"

    _append_south_row(sheet)
    doc.NamedRanges.getByName("SalesData").setContent(f"${sheet_name}.$C$5:$D$7")
    second = read_model_table_grid(doc, parsed)
    assert len(second) == 3
    assert second[2][0] == "South"
    assert parsed["named_range"] == "SalesData"
    assert parsed.get("range") is None

    if doc.NamedRanges.hasByName("SalesData"):
        doc.NamedRanges.removeByName("SalesData")


@native_test
@with_native_doc("calc")
def test_frozen_range_identity_does_not_grow(ctx, doc):
    """{range: A1} stays pinned when the sheet used-range grows."""
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = sheet.getName()
    _sales_on_sheet(sheet)
    parsed = parse_table_source_spec({"range": f"{sheet_name}.C5:D6"})

    first = read_model_table_grid(doc, parsed)
    assert len(first) == 2
    _append_south_row(sheet)
    second = read_model_table_grid(doc, parsed)
    assert len(second) == 2
    assert [row[0] for row in second] == ["Region", "North"]


@native_test
@with_native_doc("calc")
def test_database_range_identity_reads_data_area(ctx, doc):
    from com.sun.star.table import CellRangeAddress

    sheet = doc.getSheets().getByIndex(0)
    _sales_on_sheet(sheet)
    if doc.DatabaseRanges.hasByName("SalesDB"):
        doc.DatabaseRanges.removeByName("SalesDB")
    addr = CellRangeAddress()
    addr.Sheet = 0
    addr.StartColumn = 2
    addr.StartRow = 4
    addr.EndColumn = 3
    addr.EndRow = 5
    doc.DatabaseRanges.addNewByName("SalesDB", addr)

    parsed = parse_table_source_spec({"named_range": "SalesDB"})
    grid = read_model_table_grid(doc, parsed)
    assert len(grid) == 2
    assert grid[0] == ["Region", "Sales"]

    if doc.DatabaseRanges.hasByName("SalesDB"):
        doc.DatabaseRanges.removeByName("SalesDB")


@native_test
@with_native_doc("calc")
def test_query_folder_sql_sheet_identity_host_path(ctx, doc):
    """Host preload registers the catalog key from {sheet} used-range."""
    from types import SimpleNamespace
    from unittest.mock import patch

    sheet = doc.getSheets().getByIndex(0)
    _sales_on_sheet(sheet)
    captured: dict = {}

    def _fake_run(*_args, **kwargs):
        captured["preloaded"] = kwargs.get("preloaded")
        return {"status": "ok", "helper": "query_folder_sql", "total_rows": 1}

    tctx = SimpleNamespace(ctx=ctx, doc=doc, doc_type="calc", active_domain="analysis")
    with patch("plugin.scripting.client.run_folder_sql", side_effect=_fake_run):
        res = QueryFolderSqlTool().execute(
            tctx,
            sql="SELECT * FROM sales",
            tables={"sales": {"sheet": sheet.getName()}},
        )
    assert res.get("status") == "ok", res
    grid = (captured.get("preloaded") or {}).get("sales", {}).get("grid")
    assert grid is not None
    assert len(grid) == 2
    assert grid[0][0] == "Region"
