# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO integration tests for document_open, document_save, and document_export."""

import os
import shutil
import tempfile
import zipfile

import uno
import unohelper
from com.sun.star.task import XInteractionHandler

from plugin.doc.document_file_tools import DocumentExport, DocumentOpen, DocumentSave
from plugin.framework.errors import UnoObjectError
from plugin.framework.uno_context import (
    create_property_value,
    get_desktop,
    get_runtime_uid,
    resolve_document_by_url,
)
from plugin.framework.url_utils import path_to_file_url
from plugin.testing_runner import native_test
from tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("writer", reuse=False)
def test_document_save_export_open_roundtrip_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa files é ")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Hello template", False)

        # 1. Save-as DOCX
        docx_path = os.path.join(tmp_dir, "Modèle juridique.docx")
        res = DocumentSave().execute(tool_ctx, path=docx_path)
        assert res["status"] == "ok", res
        with zipfile.ZipFile(docx_path) as zf:
            assert "word/document.xml" in zf.namelist()
        assert doc.getURL() == path_to_file_url(docx_path)
        assert not doc.isModified()
        assert res["document"]["name"] == "Modèle juridique.docx"

        # 2. Modify and save in place (no path)
        cursor.gotoEnd(False)
        text.insertString(cursor, " updated", False)
        res_inplace = DocumentSave().execute(tool_ctx)
        assert res_inplace["status"] == "ok", res_inplace
        assert not doc.isModified()
        with zipfile.ZipFile(docx_path) as zf:
            assert b"updated" in zf.read("word/document.xml")

        # 3. Export PDF without changing doc URL
        pdf_path = os.path.join(tmp_dir, "out é.pdf")
        res_pdf = DocumentExport().execute(tool_ctx, path=pdf_path)
        assert res_pdf["status"] == "ok", res_pdf
        with open(pdf_path, "rb") as f:
            assert f.read(5) == b"%PDF-"
        assert doc.getURL() == path_to_file_url(docx_path)

        # 4. Open already-open file -> returns existing doc, same frame count
        desktop = get_desktop(ctx)
        frames_before = desktop.getFrames().getCount()
        res_reopen = DocumentOpen().execute(tool_ctx, path=docx_path)
        assert res_reopen["status"] == "ok", res_reopen
        assert res_reopen["already_open"] is True
        assert res_reopen["document"]["uid"] == get_runtime_uid(doc)
        assert desktop.getFrames().getCount() == frames_before

        # 5. Open as_template=True -> untitled copy with same text
        res_tpl = DocumentOpen().execute(tool_ctx, path=docx_path, as_template=True)
        assert res_tpl["status"] == "ok", res_tpl
        assert res_tpl["already_open"] is False
        assert res_tpl["document"]["url"] == ""
        tpl_model, _ = resolve_document_by_url(ctx, res_tpl["document"]["uid"])
        assert tpl_model is not None
        try:
            assert tpl_model.getText().getString() == "Hello template updated"
        finally:
            tpl_model.close(True)

        # 6. Export ODT copy and open it fresh
        copy_odt = os.path.join(tmp_dir, "copy.odt")
        res_odt = DocumentExport().execute(tool_ctx, path=copy_odt)
        assert res_odt["status"] == "ok", res_odt
        res_open_odt = DocumentOpen().execute(tool_ctx, path=copy_odt)
        assert res_open_odt["status"] == "ok", res_open_odt
        assert res_open_odt["already_open"] is False
        assert res_open_odt["document"]["url"] == path_to_file_url(copy_odt)
        assert res_open_odt["document"]["uid"] != get_runtime_uid(doc)
        odt_model, _ = resolve_document_by_url(ctx, res_open_odt["document"]["uid"])
        assert odt_model is not None
        try:
            assert odt_model.getText().getString() == "Hello template updated"
        finally:
            odt_model.close(True)

        # 7. Negative cases
        res_pdf_save = DocumentSave().execute(tool_ctx, path=os.path.join(tmp_dir, "x.pdf"))
        assert res_pdf_save["status"] == "error"
        assert res_pdf_save["code"] == "EXPORT_ONLY_FORMAT"

        res_missing = DocumentOpen().execute(tool_ctx, path=os.path.join(tmp_dir, "missing.odt"))
        assert res_missing["status"] == "error"
        assert res_missing["code"] == "FILE_NOT_FOUND"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@native_test
@with_native_doc("writer", reuse=False)
def test_document_open_html_is_writer_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa_html_")
    html_path = os.path.join(tmp_dir, "page.html")
    odt_path = os.path.join(tmp_dir, "page.odt")
    model = None
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write("<html><body><p>Initial</p></body></html>")
        tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
        res_open = DocumentOpen().execute(tool_ctx, path=html_path)
        assert res_open["status"] == "ok", res_open
        model, _ = resolve_document_by_url(ctx, res_open["document"]["uid"])
        assert res_open["document"]["doc_type"] == "writer"
        assert model is not None
        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        text.insertString(cursor, " appended", False)

        save_ctx = TestingFactory.create_context(doc=model, ctx=ctx, env="native")
        res_inplace = DocumentSave().execute(save_ctx)
        assert res_inplace["status"] == "ok", res_inplace
        assert "warning" in res_inplace
        with open(html_path, encoding="utf-8") as f:
            assert "appended" in f.read()

        res_odt = DocumentSave().execute(save_ctx, path=odt_path)
        assert res_odt["status"] == "ok", res_odt
        assert "warning" not in res_odt
    finally:
        if model is not None:
            model.close(True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


@native_test
@with_native_doc("writer", reuse=False)
def test_document_open_refuses_master_document_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa_odm_")
    odm_path = os.path.join(tmp_dir, "book.odm")
    desktop = get_desktop(ctx)
    odm_model = None
    try:
        odm_model = desktop.loadComponentFromURL(
            "private:factory/swriter/GlobalDocument",
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        odm_model.storeAsURL(
            path_to_file_url(odm_path),
            (create_property_value("FilterName", "writerglobal8"),),
        )
        odm_model.close(True)
        odm_model = None

        frames_before = desktop.getFrames().getCount()
        tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
        res = DocumentOpen().execute(tool_ctx, path=odm_path)
        assert res["status"] == "error", res
        assert res["code"] == "UNSUPPORTED_DOCUMENT"
        assert desktop.getFrames().getCount() == frames_before
    finally:
        if odm_model is not None:
            odm_model.close(True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


@native_test
@with_native_doc("calc", reuse=False)
def test_document_save_csv_warns_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa_csv_")
    csv_path = os.path.join(tmp_dir, "sheet.csv")
    ods_path = os.path.join(tmp_dir, "sheet.ods")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    try:
        sheets = doc.getSheets()
        sheets.getByIndex(0).getCellByPosition(0, 0).setString("first")

        res_csv = DocumentSave().execute(tool_ctx, path=csv_path)
        assert res_csv["status"] == "ok", res_csv
        assert "warning" in res_csv
        with open(csv_path, encoding="utf-8") as f:
            assert "first" in f.read()

        sheets.getByIndex(0).getCellByPosition(0, 0).setString("updated")
        res_inplace = DocumentSave().execute(tool_ctx)
        assert res_inplace["status"] == "ok", res_inplace
        assert "warning" in res_inplace
        with open(csv_path, "rb") as f:
            saved_bytes = f.read()
        assert b"updated" in saved_bytes

        sheets.insertNewByName("Second", 1)
        sheets.getByIndex(1).getCellByPosition(0, 0).setString("second")

        res_multi_path = DocumentSave().execute(tool_ctx, path=csv_path)
        assert res_multi_path["status"] == "error", res_multi_path
        assert res_multi_path["code"] == "SHEET_REQUIRED"

        res_multi_inplace = DocumentSave().execute(tool_ctx)
        assert res_multi_inplace["status"] == "error", res_multi_inplace
        assert res_multi_inplace["code"] == "SHEET_REQUIRED"

        with open(csv_path, "rb") as f:
            assert f.read() == saved_bytes

        res_ods = DocumentSave().execute(tool_ctx, path=ods_path)
        assert res_ods["status"] == "ok", res_ods
        assert "warning" not in res_ods
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@native_test
@with_native_doc("calc", reuse=False)
def test_document_export_csv_named_sheet_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa_csv_export_")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    try:
        sheets = doc.getSheets()
        s1 = sheets.getByIndex(0)
        cell = s1.getCellByPosition(0, 0)
        cell.setValue(1234.5)
        currency_fmt = uno.getConstantByName("com.sun.star.util.NumberFormat.CURRENCY")
        cell.NumberFormat = doc.getNumberFormats().getStandardFormat(currency_fmt, s1.CharLocale)
        assert cell.getString() != "1234.5"

        sheets.insertNewByName("Q1 2026 é", 1)
        s2 = sheets.getByIndex(1)
        s2.getCellByPosition(0, 0).setString("second-é")

        sheets.insertNewByName("HiddenSheet", 2)
        s3 = sheets.getByIndex(2)
        s3.IsVisible = False
        s3.getCellByPosition(0, 0).setString("hidden-val")

        ods_path = os.path.join(tmp_dir, "book.ods")
        doc.storeAsURL(path_to_file_url(ods_path), ())
        doc.getCurrentController().setActiveSheet(s1)

        out2 = os.path.join(tmp_dir, "out2.csv")
        res2 = DocumentExport().execute(tool_ctx, path=out2, sheet="Q1 2026 é")
        assert res2["status"] == "ok", res2
        assert os.path.isfile(out2)
        with open(out2, encoding="utf-8") as f:
            assert f.read().replace("\r\n", "\n") == "second-é\n"

        out3 = os.path.join(tmp_dir, "out3.csv")
        res3 = DocumentExport().execute(tool_ctx, path=out3, sheet="HiddenSheet")
        assert res3["status"] == "ok", res3
        with open(out3, encoding="utf-8") as f:
            assert f.read().replace("\r\n", "\n") == "hidden-val\n"

        assert not any("-" in name and name.endswith(".csv") for name in os.listdir(tmp_dir))
        assert doc.getURL() == path_to_file_url(ods_path)
        assert doc.getCurrentController().getActiveSheet().getName() == s1.getName()

        # Bytes for sheet 1 must match LibreOffice's no-options default CSV export.
        out1 = os.path.join(tmp_dir, "out1.csv")
        res1 = DocumentExport().execute(tool_ctx, path=out1, sheet=s1.getName())
        assert res1["status"] == "ok", res1
        ref1 = os.path.join(tmp_dir, "ref1.csv")
        doc.storeToURL(
            path_to_file_url(ref1),
            (create_property_value("FilterName", "Text - txt - csv (StarCalc)"),),
        )
        with open(out1, "rb") as f_got, open(ref1, "rb") as f_ref:
            assert f_got.read() == f_ref.read()

        missing_out = os.path.join(tmp_dir, "missing.csv")
        try:
            DocumentExport().execute(tool_ctx, path=missing_out, sheet="NoSuchSheet")
        except UnoObjectError:
            pass
        else:
            raise AssertionError("Expected UnoObjectError for unknown sheet name")
        assert not os.path.exists(missing_out)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@native_test
@with_native_doc("writer", reuse=False)
def test_document_export_and_save_refuse_open_paths_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa_open_refuse_")
    target = os.path.join(tmp_dir, "Report (1) é.odt")
    hidden_doc = None
    try:
        text = doc.getText()
        text.insertString(text.createTextCursor(), "original", False)
        doc.storeAsURL(uno.systemPathToFileUrl(target), ())
        with open(target, "rb") as f:
            orig_bytes = f.read()

        tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
        res_own_export = DocumentExport().execute(tool_ctx, path=target)
        assert res_own_export["status"] == "error", res_own_export
        assert res_own_export["code"] == "PATH_IS_OPEN"
        with open(target, "rb") as f:
            assert f.read() == orig_bytes

        desktop = get_desktop(ctx)
        hidden_doc = desktop.loadComponentFromURL(
            "private:factory/swriter",
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        htext = hidden_doc.getText()
        htext.insertString(htext.createTextCursor(), "intruder", False)
        hidden_ctx = TestingFactory.create_context(doc=hidden_doc, ctx=ctx, env="native")

        res_other_export = DocumentExport().execute(hidden_ctx, path=target)
        assert res_other_export["status"] == "error", res_other_export
        assert res_other_export["code"] == "PATH_IS_OPEN"
        with open(target, "rb") as f:
            assert f.read() == orig_bytes

        res_other_save = DocumentSave().execute(hidden_ctx, path=target)
        assert res_other_save["status"] == "error", res_other_save
        assert res_other_save["code"] == "PATH_IS_OPEN"
        with open(target, "rb") as f:
            assert f.read() == orig_bytes

        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        text.insertString(cursor, " edited", False)
        res_save = DocumentSave().execute(tool_ctx)
        assert res_save["status"] == "ok", res_save
    finally:
        if hidden_doc is not None:
            hidden_doc.close(True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


class _SpyInteractionHandler(unohelper.Base, XInteractionHandler):  # type: ignore[misc]
    def __init__(self) -> None:
        self.called = 0

    def handle(self, Request) -> None:
        self.called += 1
        abort_type = uno.getTypeByName("com.sun.star.task.XInteractionAbort")
        for continuation in Request.getContinuations():
            if continuation.queryInterface(abort_type) is not None:
                continuation.select()
                return


@native_test
@with_native_doc("writer", reuse=False)
def test_document_save_refuses_file_changed_on_disk_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa_changed_")
    target = os.path.join(tmp_dir, "doc.odt")
    other_target = os.path.join(tmp_dir, "doc_copy.odt")
    loaded = None
    try:
        text = doc.getText()
        text.insertString(text.createTextCursor(), "initial", False)
        doc.storeAsURL(path_to_file_url(target), ())

        spy = _SpyInteractionHandler()
        desktop = get_desktop(ctx)
        loaded = desktop.loadComponentFromURL(
            path_to_file_url(target),
            "_blank",
            0,
            (
                create_property_value("Hidden", True),
                create_property_value("InteractionHandler", spy),
            ),
        )
        assert loaded is not None
        with open(target, "rb") as f:
            orig_bytes = f.read()

        ltext = loaded.getText()
        cursor = ltext.createTextCursor()
        cursor.gotoEnd(False)
        ltext.insertString(cursor, " modified", False)

        st = os.stat(target)
        os.utime(target, (st.st_atime, st.st_mtime + 100))

        tool_ctx = TestingFactory.create_context(doc=loaded, ctx=ctx, env="native")
        for call_kwargs in ({}, {"path": target}, {}):
            res = DocumentSave().execute(tool_ctx, **call_kwargs)
            assert res["status"] == "error", res
            assert res["code"] == "FILE_CHANGED_ON_DISK"
            assert loaded.isModified()
            assert spy.called == 0
            with open(target, "rb") as f:
                assert f.read() == orig_bytes

        res_new = DocumentSave().execute(tool_ctx, path=other_target)
        assert res_new["status"] == "ok", res_new
        cursor = ltext.createTextCursor()
        cursor.gotoEnd(False)
        ltext.insertString(cursor, " more", False)
        res_again = DocumentSave().execute(tool_ctx)
        assert res_again["status"] == "ok", res_again
    finally:
        if loaded is not None:
            loaded.close(True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


@native_test
@with_native_doc("writer", reuse=False)
def test_document_export_and_save_refuse_locked_target_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa_locked_")
    target = os.path.join(tmp_dir, "t.odt")
    lock_file = os.path.join(tmp_dir, ".~lock.t.odt#")
    try:
        with open(target, "wb") as f:
            f.write(b"keep-me")
        with open(lock_file, "w", encoding="utf-8") as f:
            f.write("Someone Else,other,otherhost,24.11.2026 10:00,file:///x;")

        text = doc.getText()
        text.insertString(text.createTextCursor(), "content", False)
        orig_url = doc.getURL()
        tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

        res_export = DocumentExport().execute(tool_ctx, path=target)
        assert res_export["status"] == "error", res_export
        assert res_export["code"] == "FILE_LOCKED"
        with open(target, "rb") as f:
            assert f.read() == b"keep-me"
        assert doc.getURL() == orig_url

        res_save = DocumentSave().execute(tool_ctx, path=target)
        assert res_save["status"] == "error", res_save
        assert res_save["code"] == "FILE_LOCKED"
        with open(target, "rb") as f:
            assert f.read() == b"keep-me"
        assert doc.getURL() == orig_url
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@native_test
@with_native_doc("writer", reuse=False)
def test_document_save_own_path_keeps_password_uno(ctx, doc):
    tmp_dir = tempfile.mkdtemp(prefix="wa_pwd_")
    target = os.path.join(tmp_dir, "secret.odt")
    try:
        text = doc.getText()
        text.insertString(text.createTextCursor(), "top secret", False)
        doc.storeAsURL(
            path_to_file_url(target),
            (create_property_value("Password", "pass123"),),
        )
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        text.insertString(cursor, " edited", False)

        tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
        res = DocumentSave().execute(tool_ctx, path=target)
        assert res["status"] == "ok", res
        with zipfile.ZipFile(target) as zf:
            assert b"encryption-data" in zf.read("META-INF/manifest.xml")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
