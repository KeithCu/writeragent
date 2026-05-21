# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""UNO tests for document_research grep_nearby_files."""

from __future__ import annotations

import os
import tempfile
import unittest

import uno

from plugin.doc.document_research_grep import grep_nearby_files
from plugin.framework.uno_context import get_desktop
from plugin.main import get_services
from plugin.testing_runner import native_test, setup, show_window, teardown

_SKIP_HEADLESS = "grep_nearby_files processEventsToIdle hangs in headless testing_runner (document_research_grep.py)"

_test_ctx = None
_temp_dir = None
_active_doc = None
_budget_path = None
_writer_path = None


def _hidden_prop():
    return uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=not show_window)


@setup
def setup_grep_uno(ctx):
    global _test_ctx, _temp_dir, _active_doc, _budget_path, _writer_path
    _test_ctx = ctx
    _temp_dir = tempfile.mkdtemp(prefix="wa_grep_")
    desktop = get_desktop(ctx)

    budget = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (_hidden_prop(),))
    sheet = budget.Sheets.getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula("Revenue")
    sheet.getCellByPosition(0, 1).setFormula("Q4 total")
    sheet.getCellByPosition(1, 1).setFormula("99")

    _budget_path = os.path.join(_temp_dir, "Budget_2026.ods")
    budget.storeAsURL(uno.systemPathToFileUrl(_budget_path), ())
    budget.close(True)

    writer = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (_hidden_prop(),))
    text = writer.getText()
    cursor = text.createTextCursor()
    cursor.setString("Meeting notes without the keyword.")
    para = text.createTextCursor()
    para.gotoEnd(False)
    para.setString("\nQuarter Q4 summary paragraph.")
    _writer_path = os.path.join(_temp_dir, "Notes.odt")
    writer.storeAsURL(uno.systemPathToFileUrl(_writer_path), ())
    writer.close(True)

    _active_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (_hidden_prop(),))
    active_path = os.path.join(_temp_dir, "Report.ods")
    _active_doc.storeAsURL(uno.systemPathToFileUrl(active_path), ())


@teardown
def teardown_grep_uno(ctx):
    global _test_ctx, _temp_dir, _active_doc, _budget_path, _writer_path
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
    _writer_path = None


def _desktop_component_count(ctx) -> int:
    desktop = get_desktop(ctx)
    comps = desktop.getComponents()
    if not comps:
        return 0
    enum = comps.createEnumeration()
    n = 0
    while enum and enum.hasMoreElements():
        enum.nextElement()
        n += 1
    return n


@unittest.skipIf(not show_window, _SKIP_HEADLESS)
@native_test
def test_grep_budget_calc_hit_excludes_notes_from_subset():
    before = _desktop_component_count(_test_ctx)
    result = grep_nearby_files(
        _test_ctx,
        _active_doc,
        get_services(),
        "Q4",
        file_subset="budget",
    )
    after = _desktop_component_count(_test_ctx)

    assert result["status"] == "ok", result
    assert result["files_with_hits"] >= 1
    hit_names = {h["name"] for h in result["hits"]}
    assert "Budget_2026.ods" in hit_names
    assert "Notes.odt" not in hit_names

    budget_hit = next(h for h in result["hits"] if h["name"] == "Budget_2026.ods")
    assert budget_hit["doc_type"] == "calc"
    assert any("Q4" in m.get("value", "") for m in budget_hit["matches"])

    # Hidden opens from grep must be closed (component count unchanged).
    assert after == before


@unittest.skipIf(not show_window, _SKIP_HEADLESS)
@native_test
def test_grep_writer_paragraph_snippet():
    before = _desktop_component_count(_test_ctx)
    result = grep_nearby_files(
        _test_ctx,
        _active_doc,
        get_services(),
        "Q4",
        file_subset="notes",
    )
    after = _desktop_component_count(_test_ctx)

    assert result["status"] == "ok", result
    assert result["files_with_hits"] >= 1
    writer_hit = next(h for h in result["hits"] if h["name"] == "Notes.odt")
    assert writer_hit["doc_type"] == "writer"
    match = writer_hit["matches"][0]
    assert "paragraph_index" in match
    assert "context" in match
    assert any("Q4" in c.get("text", "") for c in match.get("context", []))

    assert after == before
