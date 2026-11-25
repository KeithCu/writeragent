# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO tests that Manage Changes survives closing hidden Writer documents."""

from __future__ import annotations

import os
import re
import tempfile
import zipfile

import uno
from com.sun.star.frame.DispatchResultState import SUCCESS
from com.sun.star.style.BreakType import PAGE_BEFORE
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

from plugin.app import get_services, get_tools
from plugin.doc.document_research import close_document_research_document, open_document_for_read
from plugin.framework.scratch_writer import close_scratch_document, new_blank_writer
from plugin.framework.tool import ToolContext
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    TestingFactory,
    note_windows_hidden_open_bitmap,
    skip_windows_hidden_open_after_bitmap,
    with_native_doc,
)
from plugin.vision.vision_preview import render_preview_page
from plugin.writer.get_document_content import GetDocumentContent


def _toggle_manage_changes(ctx, doc) -> None:
    # Tests using this helper must open doc with hidden=False: LibreOffice refuses the
    # dispatch on a hidden document, so the dialog would never open.
    frame = doc.getCurrentController().getFrame()
    helper = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    for _idx in range(2):
        result = helper.executeDispatch(frame, ".uno:AcceptTrackedChanges", "", 0, ())
        assert result.State == SUCCESS, result
    assert isinstance(doc.getText().getString(), str)


@native_test
@with_native_doc("writer", hidden=False, reuse=False)
def test_manage_changes_opens_after_scratch_writer_closes_uno(ctx, doc):
    scratch = new_blank_writer(ctx)
    assert scratch is not None
    close_scratch_document(scratch)
    _toggle_manage_changes(ctx, doc)


@native_test
@with_native_doc("writer", hidden=False, reuse=False)
def test_manage_changes_opens_after_range_read_uno(ctx, doc):
    doc.getText().setString("Hello range read")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = GetDocumentContent().execute(tool_ctx, scope="range", start=0, end=5)
    assert res.get("status") == "ok", res
    assert "Hello" in str(res.get("content", ""))
    _toggle_manage_changes(ctx, doc)


@native_test
@with_native_doc("writer", hidden=False, reuse=False)
def test_manage_changes_opens_after_vision_preview_uno(ctx, doc):
    preview = render_preview_page(ctx, "<h1>T</h1><p>x</p>", "s")
    assert preview.width_px > 0
    _toggle_manage_changes(ctx, doc)


@native_test
@with_native_doc("writer", hidden=False, reuse=False)
def test_manage_changes_opens_after_sibling_read_uno(ctx, doc):
    skip_windows_hidden_open_after_bitmap("scratch_writer Hidden sibling txt")
    fd, path = tempfile.mkstemp(prefix="wa_sibling_", suffix=".txt")
    model = None
    opened = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("Sibling document body")
        model, doc_type, err, opened = open_document_for_read(ctx, path)
        note_windows_hidden_open_bitmap(err)
        skip_windows_hidden_open_after_bitmap("scratch_writer Hidden sibling txt")
        assert err is None, err
        assert opened is True
        assert doc_type == "writer"
        assert "Sibling document body" in model.getText().getString()
    finally:
        try:
            close_document_research_document(model, opened_for_document_research=opened)
        finally:
            if os.path.exists(path):
                os.remove(path)
    _toggle_manage_changes(ctx, doc)


def _rewrite_meta_page_count(src_odt: str, dst_odt: str, count: int) -> None:
    with zipfile.ZipFile(src_odt, "r") as zin, zipfile.ZipFile(dst_odt, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "meta.xml":
                text = data.decode("utf-8")
                text, replaced = re.subn(r'meta:page-count="\d+"', f'meta:page-count="{count}"', text)
                assert replaced == 1, "meta:page-count not found in meta.xml"
                data = text.encode("utf-8")
            zout.writestr(item, data)


@native_test
@with_native_doc("writer", hidden=False, reuse=False)
def test_get_tree_on_hidden_writer_sibling_uno(ctx, doc):
    skip_windows_hidden_open_after_bitmap("scratch_writer Hidden sibling odt")
    text = doc.getText()
    cursor = text.createTextCursor()
    for i in range(1, 4):
        if i > 1:
            text.insertControlCharacter(cursor, 0, False)
            cursor.setPropertyValue("BreakType", PAGE_BEFORE)
        cursor.setPropertyValue("ParaStyleName", "Heading 1")
        text.insertString(cursor, f"Section {i}", False)
        text.insertControlCharacter(cursor, 0, False)
        cursor.setPropertyValue("ParaStyleName", "Standard")
        text.insertString(cursor, f"Body {i}", False)

    fd1, raw_path = tempfile.mkstemp(prefix="wa_sibling_raw_", suffix=".odt")
    os.close(fd1)
    fd2, stale_path = tempfile.mkstemp(prefix="wa_sibling_tree_", suffix=".odt")
    os.close(fd2)
    model = None
    opened = False
    try:
        doc.storeToURL(uno.systemPathToFileUrl(raw_path), ())
        _rewrite_meta_page_count(raw_path, stale_path, 99)
        model, doc_type, err, opened = open_document_for_read(ctx, stale_path)
        note_windows_hidden_open_bitmap(err)
        skip_windows_hidden_open_after_bitmap("scratch_writer Hidden sibling odt")
        assert err is None, err
        assert opened is True
        assert doc_type == "writer"
        services = get_services()
        # Call before get_tree so heading bookmarks cannot mask a stale meta:page-count.
        assert services.document.get_page_count(model) == 3
        tctx = ToolContext(model, ctx, "writer", services, "test", read_only_target=True)
        res = get_tools().execute("get_tree", tctx)
        assert res.get("status") == "ok", res
        assert res.get("page_count") == 3, res
        titles = [c.get("text") for c in res.get("children", [])]
        assert titles == ["Section 1", "Section 2", "Section 3"], res
    finally:
        try:
            close_document_research_document(model, opened_for_document_research=opened)
        finally:
            for p in (raw_path, stale_path):
                if os.path.exists(p):
                    os.remove(p)
    _toggle_manage_changes(ctx, doc)


@native_test
@with_native_doc("writer", hidden=False, reuse=False)
def test_manage_changes_opens_after_full_read_of_multi_paragraph_deletion_uno(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    text.insertString(cursor, "First para", False)
    text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    text.insertString(cursor, "Second para", False)
    text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    text.insertString(cursor, "Third para", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        del_cursor = text.createTextCursor()
        del_cursor.gotoStart(False)
        del_cursor.goRight(6, False)
        del_cursor.goRight(20, True)
        del_cursor.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = GetDocumentContent().execute(tool_ctx, scope="full")
    assert res.get("status") == "ok", res
    content = str(res.get("content", ""))
    assert "Second para" not in content, res
    assert "First" in content and "para" in content, res
    _toggle_manage_changes(ctx, doc)
