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
    # open hung 30s. Product named target did not help.
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


@native_test
@with_native_doc("calc")
def test_research_read_stays_out_of_catalog_uno(ctx, doc):
    import unohelper
    from com.sun.star.document import XDocumentEventListener

    from plugin.doc.document_research import close_document_research_document, get_open_documents
    from plugin.framework.scratch_writer import is_scratch_document, run_after_scratch_loads
    from plugin.framework.uno_context import get_runtime_uid

    skip_windows_hidden_open_after_bitmap("document_research Hidden Draw")
    temp_dir = tempfile.mkdtemp(prefix="wa_draw_read_")
    draw_path = os.path.join(temp_dir, "deck.fodg")
    with open(draw_path, "w", encoding="utf-8") as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"\n'
            '  xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"\n'
            '  office:version="1.2"\n'
            '  office:mimetype="application/vnd.oasis.opendocument.graphics">\n'
            '  <office:body><office:drawing><draw:page draw:name="p1"/></office:drawing></office:body>\n'
            '</office:document>\n'
        )

    seen_types: list[frozenset[str]] = []
    doc_service = get_services().document

    class _Listener(unohelper.Base, XDocumentEventListener):
        def documentEventOccured(self, _event):
            run_after_scratch_loads(lambda: seen_types.append(doc_service.open_doc_types()))

        def disposing(self, _event):
            pass

    broadcaster = ctx.getServiceManager().createInstanceWithContext(
        "com.sun.star.frame.GlobalEventBroadcaster", ctx
    )
    listener = _Listener()
    broadcaster.addDocumentEventListener(listener)
    model = None
    opened = False
    try:
        model, doc_type, err, opened = open_document_for_read(ctx, draw_path)
        note_windows_hidden_open_bitmap(err)
        skip_windows_hidden_open_after_bitmap("document_research Hidden Draw")
        assert err is None
        assert doc_type == "draw"
        assert opened is True
        assert is_scratch_document(model) is True
        uid = get_runtime_uid(model)
        assert uid
        assert all(entry["uid"] != uid for entry in get_open_documents(ctx, doc))
        close_document_research_document(model, opened_for_document_research=opened)
        model = None
        assert seen_types
        assert all("draw" not in types for types in seen_types)
    finally:
        broadcaster.removeDocumentEventListener(listener)
        if model is not None:
            close_document_research_document(model, opened_for_document_research=opened)
        _cleanup_nearby_test_env(temp_dir)


@native_test
@with_native_doc("calc")
def test_research_read_never_adopts_an_open_document_uno(ctx, doc):
    import unittest
    from unittest.mock import patch

    from plugin.doc.document_research import close_document_research_document
    from plugin.framework.uno_context import get_runtime_uid

    if sys.platform == "win32":
        # GHA 34639913692: Hidden load of the pooled Calc's own storeAsURL path
        # raises "Could not create system bitmap!" and hangs the next Hidden open.
        raise unittest.SkipTest("Windows Hidden open of pooled Calc URL hits system bitmap error")

    temp_dir = tempfile.mkdtemp(prefix="wa_open_calc_")
    calc_path = os.path.join(temp_dir, "saved.ods")
    model = None
    opened = False
    try:
        sheet = doc.Sheets.getByIndex(0)
        sheet.getCellByPosition(0, 0).setString("saved")
        doc.storeAsURL(uno.systemPathToFileUrl(calc_path), ())
        sheet.getCellByPosition(0, 0).setString("unsaved edit")
        user_uid = get_runtime_uid(doc)

        with patch("plugin.doc.document_research.resolve_document_by_url", return_value=(None, None)):
            model, doc_type, err, opened = open_document_for_read(ctx, calc_path)
        note_windows_hidden_open_bitmap(err)
        skip_windows_hidden_open_after_bitmap("document_research Hidden open calc")
        assert err is None
        assert doc_type == "calc"
        assert opened is True
        assert get_runtime_uid(model) != user_uid
        assert model.Sheets.getByIndex(0).getCellByPosition(0, 0).getString() == "saved"

        close_document_research_document(model, opened_for_document_research=opened)
        model = None
        assert get_runtime_uid(doc) == user_uid
        assert doc.Sheets.getByIndex(0).getCellByPosition(0, 0).getString() == "unsaved edit"
    finally:
        if model is not None:
            close_document_research_document(model, opened_for_document_research=opened)
        _cleanup_nearby_test_env(temp_dir)


@native_test
@with_native_doc("calc")
def test_user_open_during_research_read_survives_uno(ctx, doc):
    import unittest

    from plugin.doc.document_research import close_document_research_document, get_open_documents
    from plugin.framework.uno_context import get_runtime_uid, resolve_document_by_url

    if sys.platform == "win32":
        raise unittest.SkipTest("Windows Hidden open test")

    temp_dir = tempfile.mkdtemp(prefix="wa_user_open_")
    txt_path = os.path.join(temp_dir, "note.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("hello world")
    url = uno.systemPathToFileUrl(txt_path)

    research_model = None
    opened = False
    user_doc = None
    try:
        research_model, doc_type, err, opened = open_document_for_read(ctx, txt_path)
        assert err is None
        assert doc_type == "writer"
        assert opened is True

        desktop = get_desktop(ctx)
        user_doc = desktop.loadComponentFromURL(url, "_default", 0, ())
        assert user_doc is not None
        user_uid = get_runtime_uid(user_doc)
        assert user_uid != get_runtime_uid(research_model)
        assert user_doc.getCurrentController().getFrame().getContainerWindow().isVisible() is True
        assert user_doc.isReadonly() is False

        resolved_doc, _ = resolve_document_by_url(ctx, url)
        assert get_runtime_uid(resolved_doc) == user_uid
        assert any(entry["uid"] == user_uid for entry in get_open_documents(ctx, doc))

        close_document_research_document(research_model, opened_for_document_research=opened)
        research_model = None
        assert "hello world" in user_doc.getText().getString()
    finally:
        if research_model is not None:
            close_document_research_document(research_model, opened_for_document_research=opened)
        if user_doc is not None:
            try:
                user_doc.close(True)
            except Exception:
                pass
        _cleanup_nearby_test_env(temp_dir)


@native_test
@with_native_doc("calc")
def test_document_open_during_research_read_uno(ctx, doc):
    import unittest

    from plugin.doc.document_research import close_document_research_document
    from plugin.framework.uno_context import get_runtime_uid, resolve_document_by_url

    if sys.platform == "win32":
        raise unittest.SkipTest("Windows Hidden open test")

    temp_dir = tempfile.mkdtemp(prefix="wa_doc_open_")
    txt_path = os.path.join(temp_dir, "doc_open.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("via document_open")
    url = uno.systemPathToFileUrl(txt_path)

    research_model = None
    opened = False
    user_doc = None
    try:
        research_model, _doc_type, err, opened = open_document_for_read(ctx, txt_path)
        assert err is None
        assert opened is True

        tctx = ToolContext(doc, ctx, "calc", get_services(), "test")
        res = get_tools().execute("document_open", tctx, path=txt_path)
        assert res.get("status") == "ok", res
        assert res.get("already_open") is False

        user_doc, _ = resolve_document_by_url(ctx, url)
        assert user_doc is not None
        assert get_runtime_uid(user_doc) != get_runtime_uid(research_model)

        close_document_research_document(research_model, opened_for_document_research=opened)
        research_model = None
        assert "via document_open" in user_doc.getText().getString()
    finally:
        if research_model is not None:
            close_document_research_document(research_model, opened_for_document_research=opened)
        if user_doc is not None:
            try:
                user_doc.close(True)
            except Exception:
                pass
        _cleanup_nearby_test_env(temp_dir)


@native_test
def test_overlapping_research_reads_are_independent_uno(ctx):
    import unittest

    from plugin.doc.document_research import close_document_research_document
    from plugin.framework.uno_context import get_runtime_uid

    if sys.platform == "win32":
        raise unittest.SkipTest("Windows Hidden open test")

    temp_dir = tempfile.mkdtemp(prefix="wa_overlap_")
    txt_path = os.path.join(temp_dir, "overlap.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("shared content")

    m1 = m2 = None
    o1 = o2 = False
    try:
        m1, _t1, e1, o1 = open_document_for_read(ctx, txt_path)
        m2, _t2, e2, o2 = open_document_for_read(ctx, txt_path)
        assert e1 is None and e2 is None
        assert o1 is True and o2 is True
        assert get_runtime_uid(m1) != get_runtime_uid(m2)

        close_document_research_document(m1, opened_for_document_research=o1)
        m1 = None
        assert "shared content" in m2.getText().getString()
    finally:
        if m1 is not None:
            close_document_research_document(m1, opened_for_document_research=o1)
        if m2 is not None:
            close_document_research_document(m2, opened_for_document_research=o2)
        _cleanup_nearby_test_env(temp_dir)


@native_test
def test_research_close_keeps_user_python_session_uno(ctx):
    import unittest
    from unittest.mock import patch

    from plugin.calc.python import workbook_lifecycle
    from plugin.doc.document_research import close_document_research_document
    from plugin.tests.testing_utils import TestingFactory

    if sys.platform == "win32":
        raise unittest.SkipTest("Windows Hidden open test")

    temp_dir = tempfile.mkdtemp(prefix="wa_py_session_")
    ods_path = os.path.join(temp_dir, "shared.ods")
    url = uno.systemPathToFileUrl(ods_path)

    with TestingFactory.native_doc(ctx, "calc", reuse=False) as seed_doc:
        seed_doc.storeAsURL(url, ())

    had_global = workbook_lifecycle._global_listener is not None
    if not had_global:
        workbook_lifecycle.install_global_document_event_listener(ctx)

    research_model = None
    opened = False
    user_doc = None
    try:
        research_model, doc_type, err, opened = open_document_for_read(ctx, ods_path)
        assert err is None
        assert doc_type == "calc"
        assert opened is True

        user_doc = get_desktop(ctx).loadComponentFromURL(url, "_default", 0, ())
        assert user_doc is not None

        expected_sid = f"calc:{url}"
        shared_listeners = [
            listener
            for listener in workbook_lifecycle._LISTENERS.values()
            if listener._workbook_session_id == expected_sid
        ]
        assert len(shared_listeners) == 2

        with patch("plugin.calc.python.workbook_lifecycle.reset_python_session", return_value={"status": "ok"}) as reset_mock:
            close_document_research_document(research_model, opened_for_document_research=opened)
            research_model = None
            reset_mock.assert_not_called()

            user_doc.close(True)
            user_doc = None
            reset_mock.assert_called_once()
    finally:
        if research_model is not None:
            close_document_research_document(research_model, opened_for_document_research=opened)
        if user_doc is not None:
            try:
                user_doc.close(True)
            except Exception:
                pass
        if not had_global and workbook_lifecycle._global_listener is not None:
            try:
                broadcaster = ctx.getServiceManager().createInstanceWithContext(
                    "com.sun.star.frame.GlobalEventBroadcaster", ctx
                )
                broadcaster.removeDocumentEventListener(workbook_lifecycle._global_listener)
            except Exception:
                pass
            workbook_lifecycle._global_listener = None
        _cleanup_nearby_test_env(temp_dir)
