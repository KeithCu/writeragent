# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc DataPilot (pivot table) tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.calc.pivot import ListPivotTables


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


def test_list_pivot_tables_sheet_not_found():
    doc = MagicMock()
    sheets = MagicMock()
    doc.getSheets.return_value = sheets
    sheets.hasByName.return_value = False

    ctx = MagicMock()
    ctx.doc = doc

    tool = ListPivotTables()
    res = tool.execute(ctx, sheet="NoSuchSheet")

    assert res["status"] == "error"
    assert "No sheet named 'NoSuchSheet'" in res["message"]
