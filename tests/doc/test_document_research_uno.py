# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""UNO tests for document_research nearby file discovery and read-only open."""

from __future__ import annotations

import os
import tempfile

import uno

from plugin.doc.document_research import list_nearby_files, open_document_for_read
from plugin.framework.tool import ToolContext
from plugin.main import get_services, get_tools
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _create_nearby_test_env(ctx, active_doc):
    from plugin.testing_runner import _progress

    temp_dir = tempfile.mkdtemp(prefix="wa_nearby_")
    _progress("nearby_env: create budget calc start")
    budget = TestingFactory.create_native_doc(ctx, "calc", hidden=True)
    _progress("nearby_env: create budget calc done")
    sheet = budget.Sheets.getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula("100")
    sheet.getCellByPosition(0, 1).setFormula("Q4")
    sheet.getCellByPosition(1, 1).setFormula("42")

    budget_path = os.path.join(temp_dir, "Budget_2026.ods")
    _progress("nearby_env: storeAsURL budget start")
    budget.storeAsURL(uno.systemPathToFileUrl(budget_path), ())
    _progress("nearby_env: storeAsURL budget done")
    _progress("nearby_env: close_doc budget start")
    TestingFactory.close_doc(budget)
    _progress("nearby_env: close_doc budget done")

    active_path = os.path.join(temp_dir, "Report.ods")
    _progress("nearby_env: storeAsURL active start")
    active_doc.storeAsURL(uno.systemPathToFileUrl(active_path), ())
    _progress("nearby_env: storeAsURL active done")

    return temp_dir, budget_path


def _cleanup_nearby_test_env(temp_dir):
    if temp_dir and os.path.isdir(temp_dir):
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
def test_list_nearby_excludes_active(ctx, doc):
    temp_dir, _ = _create_nearby_test_env(ctx, doc)
    try:
        from plugin.testing_runner import _progress

        _progress("nearby_env: list_nearby_files start")
        result = list_nearby_files(ctx, doc)
        _progress("nearby_env: list_nearby_files done status=%s" % result.get("status"))
        assert result["status"] == "ok"
        names = {f["name"] for f in result["files"]}
        assert "Budget_2026.ods" in names
        assert "Report.ods" not in names
    finally:
        _cleanup_nearby_test_env(temp_dir)


@native_test
@with_native_doc("calc")
def test_open_document_for_read_hidden_readonly(ctx, doc):
    temp_dir, budget_path = _create_nearby_test_env(ctx, doc)
    try:
        model, doc_type, err, opened_for_document_research = open_document_for_read(ctx, budget_path)
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
    finally:
        _cleanup_nearby_test_env(temp_dir)


@native_test
@with_native_doc("calc")
def test_inner_read_cell_range_on_opened_sibling(ctx, doc):
    """Outer document_research path opens sibling; inner uses read_cell_range (no live LLM)."""
    temp_dir, budget_path = _create_nearby_test_env(ctx, doc)
    try:
        model, doc_type, err, _opened = open_document_for_read(ctx, budget_path)
        assert err is None and doc_type == "calc"
        try:
            tctx = ToolContext(model, ctx, "calc", get_services(), "test", read_only_target=True)
            result = get_tools().execute("read_cell_range", tctx, range=["B2"])
            assert result.get("status") == "ok", result
            cell_data = result.get("result")
            assert cell_data is not None
        finally:
            try:
                model.close(True)
            except Exception:
                pass
    finally:
        _cleanup_nearby_test_env(temp_dir)
