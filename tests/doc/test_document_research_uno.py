# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""UNO tests for document_research nearby file discovery and read-only open."""

from __future__ import annotations

import os
import tempfile

import uno

from plugin.doc.document_research import list_nearby_files, open_document_for_read
from plugin.framework.tool import ToolContext
from plugin.framework.uno_context import get_desktop
from plugin.main import get_services, get_tools
from plugin.testing_runner import native_test, setup, teardown

_test_ctx = None
_temp_dir = None
_active_doc = None
_budget_path = None


def _hidden_prop():
    return uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)


@setup
def setup_nearby_uno(ctx):
    global _test_ctx, _temp_dir, _active_doc, _budget_path
    _test_ctx = ctx
    _temp_dir = tempfile.mkdtemp(prefix="wa_nearby_")
    desktop = get_desktop(ctx)

    budget = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (_hidden_prop(),))
    sheet = budget.Sheets.getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula("100")
    sheet.getCellByPosition(0, 1).setFormula("Q4")
    sheet.getCellByPosition(1, 1).setFormula("42")

    _budget_path = os.path.join(_temp_dir, "Budget_2026.ods")
    budget.storeAsURL(uno.systemPathToFileUrl(_budget_path), ())
    budget.close(True)

    _active_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (_hidden_prop(),))
    active_path = os.path.join(_temp_dir, "Report.ods")
    _active_doc.storeAsURL(uno.systemPathToFileUrl(active_path), ())


@teardown
def teardown_nearby_uno(ctx):
    global _test_ctx, _temp_dir, _active_doc, _budget_path
    if _active_doc:
        try:
            _active_doc.close(True)
        except Exception:
            pass
    _active_doc = None
    _test_ctx = None
    if _temp_dir and os.path.isdir(_temp_dir):
        for name in os.listdir(_temp_dir):
            try:
                os.remove(os.path.join(_temp_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(_temp_dir)
        except OSError:
            pass
    _temp_dir = None
    _budget_path = None


@native_test
def test_list_nearby_excludes_active():
    result = list_nearby_files(_test_ctx, _active_doc)
    assert result["status"] == "ok"
    names = {f["name"] for f in result["files"]}
    assert "Budget_2026.ods" in names
    assert "Report.ods" not in names


@native_test
def test_open_document_for_read_hidden_readonly():
    model, doc_type, err, opened_for_document_research = open_document_for_read(_test_ctx, _budget_path)
    assert err is None
    assert doc_type == "calc"
    assert model is not None
    assert opened_for_document_research is True
    try:
        sheet = model.Sheets.getByIndex(0)
        val = sheet.getCellByPosition(1, 1).getValue()
        assert val == 42.0
    finally:
        try:
            model.close(True)
        except Exception:
            pass


@native_test
def test_inner_read_cell_range_on_opened_sibling():
    """Outer document_research path opens sibling; inner uses read_cell_range (no live LLM)."""
    model, doc_type, err, _opened = open_document_for_read(_test_ctx, _budget_path)
    assert err is None and doc_type == "calc"
    try:
        tctx = ToolContext(model, _test_ctx, "calc", get_services(), "test", read_only_target=True)
        result = get_tools().execute("read_cell_range", tctx, range_name=["B2"])
        assert result.get("status") == "ok", result
        cell_data = result.get("result")
        assert cell_data is not None
    finally:
        try:
            model.close(True)
        except Exception:
            pass
