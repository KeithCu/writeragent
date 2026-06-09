# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for convert_spreadsheet_to_python tool."""

from __future__ import annotations

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc = None
_test_ctx = None


@setup
def setup_import_tool_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))


@teardown
def teardown_import_tool_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


def _execute_tool(name, args):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(_test_doc, _test_ctx, "calc", get_services(), "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except (KeyError, ValueError) as e:
        res = {"status": "error", "error": str(e)}
    return res


@native_test
def test_convert_spreadsheet_to_python_basic():
    sheet = _test_doc.getCurrentController().getActiveSheet()

    # Populate grid
    sheet.getCellByPosition(0, 0).setValue(10)  # A1
    sheet.getCellByPosition(0, 1).setValue(20)  # A2
    sheet.getCellByPosition(1, 0).setFormula("=A1+A2")  # B1
    sheet.getCellByPosition(1, 1).setFormula("=SUM(A1:A2)")  # B2

    res = _execute_tool(
        "convert_spreadsheet_to_python",
        {
            "scope": "sheet",
            "output_mode": "new_sheet",
            "vectorize": False,
            "verify": False,
        },
    )

    assert res.get("status") == "ok", f"Tool failed: {res}"
    report = res.get("report", {})
    assert len(report.get("converted", [])) >= 2, f"Expected conversion, got report: {report}"

    # Verify sheet PythonImport was created
    sheets = _test_doc.getSheets()
    assert sheets.hasByName("PythonImport")
    target_sheet = sheets.getByName("PythonImport")

    # Check that formulas became =PY(...)
    assert "=PY(" in target_sheet.getCellByPosition(1, 0).getFormula().upper()
    assert "=PY(" in target_sheet.getCellByPosition(1, 1).getFormula().upper()
