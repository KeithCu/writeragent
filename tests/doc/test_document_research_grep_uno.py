# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""UNO tests for document_research grep_nearby_files."""

from __future__ import annotations

import os
import tempfile

import uno

from plugin.doc.document_research_grep import grep_nearby_files
from plugin.framework.uno_context import get_desktop
from plugin.main import get_services
from plugin.testing_runner import native_test, show_window
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _create_grep_test_env(ctx, active_doc):
    temp_dir = tempfile.mkdtemp(prefix="wa_grep_")
    hidden = not show_window

    budget = TestingFactory.create_native_doc(ctx, "calc", hidden=hidden)
    sheet = budget.Sheets.getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula("Revenue")
    sheet.getCellByPosition(0, 1).setFormula("Q4 total")
    sheet.getCellByPosition(1, 1).setFormula("99")

    budget_path = os.path.join(temp_dir, "Budget_2026.ods")
    budget.storeAsURL(uno.systemPathToFileUrl(budget_path), ())
    TestingFactory.close_doc(budget)

    writer = TestingFactory.create_native_doc(ctx, "writer", hidden=hidden)
    text = writer.getText()
    cursor = text.createTextCursor()
    cursor.setString("Meeting notes without the keyword.")
    para = text.createTextCursor()
    para.gotoEnd(False)
    para.setString("\nQuarter Q4 summary paragraph.")
    writer_path = os.path.join(temp_dir, "Notes.odt")
    writer.storeAsURL(uno.systemPathToFileUrl(writer_path), ())
    TestingFactory.close_doc(writer)

    active_path = os.path.join(temp_dir, "Report.ods")
    active_doc.storeAsURL(uno.systemPathToFileUrl(active_path), ())

    return temp_dir


def _cleanup_grep_test_env(temp_dir):
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


@native_test
@with_native_doc("calc", hidden=not show_window)
def test_grep_budget_calc_hit_excludes_notes_from_subset(ctx, doc):
    temp_dir = _create_grep_test_env(ctx, doc)
    try:
        before = _desktop_component_count(ctx)
        result = grep_nearby_files(
            ctx,
            doc,
            get_services(),
            "Q4",
            file_subset="budget",
        )
        after = _desktop_component_count(ctx)

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
    finally:
        _cleanup_grep_test_env(temp_dir)


@native_test
@with_native_doc("calc", hidden=not show_window)
def test_grep_writer_paragraph_snippet(ctx, doc):
    temp_dir = _create_grep_test_env(ctx, doc)
    try:
        before = _desktop_component_count(ctx)
        result = grep_nearby_files(
            ctx,
            doc,
            get_services(),
            "Q4",
            file_subset="notes",
        )
        after = _desktop_component_count(ctx)

        assert result["status"] == "ok", result
        assert result["files_with_hits"] >= 1
        writer_hit = next(h for h in result["hits"] if h["name"] == "Notes.odt")
        assert writer_hit["doc_type"] == "writer"
        match = writer_hit["matches"][0]
        assert "paragraph_index" in match
        assert "context" in match
        assert any("Q4" in c.get("text", "") for c in match.get("context", []))

        assert after == before
    finally:
        _cleanup_grep_test_env(temp_dir)
