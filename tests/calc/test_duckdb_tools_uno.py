# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for DuckDB sibling spreadsheet used-range ingress."""

from __future__ import annotations

import os
import tempfile

import uno

from plugin.calc.duckdb_tools import _read_sibling_office_file_as_grid
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
