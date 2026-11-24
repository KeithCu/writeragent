# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""UNO tests for document_research nearby file discovery and read-only open."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import uno

from plugin.doc.document_research import list_nearby_files, open_document_for_read
from plugin.framework.tool import ToolContext
from plugin.framework.uno_context import create_property_value, get_desktop
from plugin.app import get_services, get_tools
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    note_windows_hidden_open_bitmap,
    reset_native_doc,
    skip_windows_hidden_open_after_bitmap,
    with_native_doc,
)


def _nearby_progress(msg: str) -> None:
    """Name the last UNO call if Windows GHA fails without a Python traceback."""
    from plugin.testing_runner import _progress_verbose as _progress

    _progress("document_research_uno: %s" % msg)


def _create_nearby_test_env(ctx, active_doc):
    temp_dir = tempfile.mkdtemp(prefix="wa_nearby_")

    # GHA 34593327841 / 34599838644 / 34633295036: a second
    # private:factory/scalc under leftover paste Writers (uids 26/27,
    # frames -) failed in ~1s (PyUNO traceback wrap) and the next
    # unique _wa_factory_N hung 30s. Budget is only needed as a sibling
    # file. Write it on the pooled @with_native_doc Calc, store, wipe,
    # then store Report. Do not open a second factory Calc.
    _nearby_progress("store budget via active start")
    sheet = active_doc.Sheets.getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula("100")
    sheet.getCellByPosition(0, 1).setFormula("Q4")
    sheet.getCellByPosition(1, 1).setFormula("42")

    budget_path = os.path.join(temp_dir, "Budget_2026.ods")
    active_doc.storeAsURL(uno.systemPathToFileUrl(budget_path), ())
    _nearby_progress("store budget via active done")

    # GHA 34639913692: Hidden+ReadOnly load of this same storeAsURL path
    # raised ``Could not create system bitmap!`` then the next sibling
    # open hung 30s. Product ``_wa_doc_research`` did not help.
    # 34602219973 passed 3/3 when Budget lived on a *closed* factory
    # Calc — a URL this pooled component never owned. Copy so the
    # Hidden load is not the live document's recent URL. Do not open a
    # second factory Calc (34633295036). Do not close leftover paste
    # Writers (34556185752). Windows defers the leftover-creating
    # paste suites until after this file so Hidden-open stays 3/3
    # (34648929578). Still attempt Hidden-open (34652644656 was 3/3).
    # GHA 34655847157: leftovers=0 and the copy still bitmap-failed;
    # the next sibling Hidden open hung 30s. After bitmap, skip later
    # Hidden-opens — do not hang.
    open_path = budget_path
    if sys.platform == "win32":
        open_path = os.path.join(temp_dir, "Budget_read.ods")
        shutil.copy2(budget_path, open_path)
        _nearby_progress("copied budget for hidden open")

    reset_native_doc(active_doc, "calc", ctx)

    active_path = os.path.join(temp_dir, "Report.ods")
    _nearby_progress("store active start")
    active_doc.storeAsURL(uno.systemPathToFileUrl(active_path), ())
    _nearby_progress("store active done")

    return temp_dir, budget_path, open_path


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
    temp_dir, _unused_budget, _unused_open = _create_nearby_test_env(ctx, doc)
    try:
        _nearby_progress("research_list_nearby_files start")
        result = list_nearby_files(ctx, doc)
        _nearby_progress("research_list_nearby_files done status=%s" % result.get("status"))
        assert result["status"] == "ok"
        names = {f["name"] for f in result["files"]}
        assert "Budget_2026.ods" in names
        assert "Report.ods" not in names
    finally:
        _cleanup_nearby_test_env(temp_dir)


@native_test
@with_native_doc("calc")
def test_open_document_for_read_hidden_readonly(ctx, doc):
    skip_windows_hidden_open_after_bitmap("document_research Hidden Budget_read")
    temp_dir, _unused_budget, open_path = _create_nearby_test_env(ctx, doc)
    try:
        _nearby_progress("open_document_for_read start")
        model, doc_type, err, opened_for_document_research = open_document_for_read(ctx, open_path)
        _nearby_progress("open_document_for_read done err=%s" % (err or "-"))
        note_windows_hidden_open_bitmap(err)
        skip_windows_hidden_open_after_bitmap("document_research Hidden Budget_read")
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
    """Outer document_research path opens sibling; inner uses read_range (no live LLM)."""
    skip_windows_hidden_open_after_bitmap("document_research Hidden Budget_read")
    temp_dir, _unused_budget, open_path = _create_nearby_test_env(ctx, doc)
    try:
        model, doc_type, err, unused_opened = open_document_for_read(ctx, open_path)
        note_windows_hidden_open_bitmap(err)
        skip_windows_hidden_open_after_bitmap("document_research Hidden Budget_read")
        assert err is None and doc_type == "calc"
        try:
            tctx = ToolContext(model, ctx, "calc", get_services(), "test", read_only_target=True)
            result = get_tools().execute("read_range", tctx, range=["B2"])
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


@native_test
@with_native_doc("writer")
def test_open_document_for_read_html_and_already_open_web_uno(ctx, doc):
    skip_windows_hidden_open_after_bitmap("document_research Hidden HTML")
    temp_dir = tempfile.mkdtemp(prefix="wa_html_read_")
    html_path = os.path.join(temp_dir, "page.html")
    web_model = None
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write("<html><body><p>Hello HTML</p></body></html>")

        model, doc_type, err, opened = open_document_for_read(ctx, html_path)
        note_windows_hidden_open_bitmap(err)
        skip_windows_hidden_open_after_bitmap("document_research Hidden HTML")
        assert err is None
        assert doc_type == "writer"
        assert model is not None
        assert opened is True
        try:
            assert "Hello HTML" in model.getText().getString()
        finally:
            model.close(True)

        # Open as Writer/Web (default filter) and verify open_document_for_read refuses without closing it.
        desktop = get_desktop(ctx)
        web_model = desktop.loadComponentFromURL(
            uno.systemPathToFileUrl(html_path),
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        assert web_model is not None
        frames_before = desktop.getFrames().getCount()
        err_model, err_type, err_msg, err_opened = open_document_for_read(ctx, html_path)
        assert err_model is None
        assert err_type is None
        assert err_msg is not None and "already open" in err_msg
        assert err_opened is False
        assert desktop.getFrames().getCount() == frames_before
    finally:
        if web_model is not None:
            try:
                web_model.close(True)
            except Exception:
                pass
        _cleanup_nearby_test_env(temp_dir)
