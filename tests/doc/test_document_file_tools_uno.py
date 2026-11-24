# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO integration tests for document_open, document_save, and document_export."""

import os
import shutil
import tempfile
import zipfile

from plugin.doc.document_file_tools import DocumentExport, DocumentOpen, DocumentSave
from plugin.framework.uno_context import get_desktop, get_runtime_uid, resolve_document_by_url
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
