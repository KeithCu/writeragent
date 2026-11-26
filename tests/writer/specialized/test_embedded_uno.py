# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native LibreOffice tests for embedded Calc spreadsheet insert."""

from __future__ import annotations

import os
import tempfile
from typing import Any

# storeToURL + two Hidden reloads can exceed the default 30s UNO watchdog on CI.
os.environ.setdefault("WRITERAGENT_UNO_TEST_TIMEOUT", "120")

import uno

from plugin.framework.uno_context import create_property_value, get_desktop
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    native_writer_tool_context,
    skip_windows_hidden_open_after_bitmap,
    with_native_doc,
)
from plugin.writer.specialized.embedded import EmbeddedInsert


def _sheet_values(doc: Any, name: str) -> tuple[Any, int, int]:
    eo = doc.getEmbeddedObjects()
    assert eo.hasByName(name), name
    emb = eo.getByName(name)
    inner = emb.getEmbeddedObject()
    sheet = inner.getSheets().getByIndex(0)
    height = int(getattr(emb, "Height", 0) or 0)
    return sheet, inner, height


@native_test
@with_native_doc("writer")
def test_embedded_insert_persists_odt_and_docx(ctx, doc):
    temp_dir = tempfile.mkdtemp(prefix="wa_embed_")
    odt_path = os.path.join(temp_dir, "sales.odt")
    docx_path = os.path.join(temp_dir, "sales.docx")
    try:
        data = [
            ["Product", "Qty", "Price", "Line"],
            ["Widget", 2, 10, "=B2*C2"],
            ["Gadget", 1, 25, "=B3*C3"],
            ["Cable", 5, 3, "=B4*C4"],
            ["Adapter", 2, 8, "=B5*C5"],
            ["Total", "", "", "=SUM(D2:D5)"],
        ]
        tctx = native_writer_tool_context(doc, ctx)
        inserted = EmbeddedInsert().execute(
            tctx,
            object_type="spreadsheet",
            target="end",
            name="Sales",
            data=data,
        )
        assert inserted.get("status") == "ok", inserted
        assert inserted.get("name") == "Sales"
        assert inserted.get("rows") == 6
        assert inserted.get("cols") == 4
        assert inserted.get("height", 0) > 2260, inserted

        sheet, inner, live_height = _sheet_values(doc, "Sales")
        assert sheet.getCellByPosition(0, 0).getString() == "Product"
        assert sheet.getCellByPosition(1, 1).getValue() == 2.0
        assert sheet.getCellByPosition(3, 5).getFormula().startswith("=SUM")

        filter_odt = (create_property_value("FilterName", "writer8"),)
        doc.storeToURL(uno.systemPathToFileUrl(odt_path), filter_odt)

        skip_windows_hidden_open_after_bitmap("embedded_insert reload odt")
        desktop = get_desktop(ctx)
        assert desktop is not None
        hidden = (create_property_value("Hidden", True),)
        reloaded = desktop.loadComponentFromURL(uno.systemPathToFileUrl(odt_path), "_blank", 0, hidden)
        try:
            rsheet, _, reloaded_height = _sheet_values(reloaded, "Sales")
            assert rsheet.getCellByPosition(0, 1).getString() == "Widget"
            assert rsheet.getCellByPosition(3, 5).getFormula().startswith("=SUM")
            assert reloaded_height > 2260
        finally:
            reloaded.close(True)

        filter_docx = (create_property_value("FilterName", "Office Open XML Text"),)
        doc.storeToURL(uno.systemPathToFileUrl(docx_path), filter_docx)

        skip_windows_hidden_open_after_bitmap("embedded_insert reload docx")
        docx_doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(docx_path), "_blank", 0, hidden)
        try:
            names = docx_doc.getEmbeddedObjects().getElementNames()
            assert len(names) >= 1
            # .docx export drops the container name; pick the spreadsheet embed.
            sheet_docx = None
            for n in names:
                emb = docx_doc.getEmbeddedObjects().getByName(n)
                try:
                    inner = emb.getEmbeddedObject()
                    if inner.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
                        sheet_docx = inner.getSheets().getByIndex(0)
                        break
                except Exception:
                    continue
            assert sheet_docx is not None
            assert sheet_docx.getCellByPosition(0, 1).getString() == "Widget"
            assert sheet_docx.getCellByPosition(3, 5).getFormula().startswith("=SUM")
        finally:
            docx_doc.close(True)
    finally:
        for path in (odt_path, docx_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


@native_test
@with_native_doc("writer")
def test_embedded_insert_search_while_recording_accept_and_reject_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    text = doc.getText()
    tctx = native_writer_tool_context(doc, ctx)

    for accept in (False, True):
        text.setString("hello MATCH world")
        doc.setPropertyValue("RecordChanges", True)
        try:
            res = EmbeddedInsert().execute(
                tctx,
                object_type="spreadsheet",
                target="search",
                old_content="MATCH",
                name=f"RecSheet_{accept}",
                data=[["x"]],
            )
            assert res["status"] == "ok", res
            assert res["tracked"] is True and res["pending_review"] is True
            cmd = ".uno:AcceptAllTrackedChanges" if accept else ".uno:RejectAllTrackedChanges"
            execute_dispatch(ctx, doc.getCurrentController(), cmd)
        finally:
            doc.setPropertyValue("RecordChanges", False)

        if accept:
            assert "MATCH" not in text.getString()
            assert doc.getEmbeddedObjects().hasByName(f"RecSheet_{accept}")
        else:
            assert text.getString() == "hello MATCH world"
            assert not doc.getEmbeddedObjects().hasByName(f"RecSheet_{accept}")


@native_test
@with_native_doc("writer", reuse=False)
def test_embedded_insert_with_at_page_ole_selected_uno(ctx, doc):
    from com.sun.star.style.BreakType import PAGE_BEFORE
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
    from com.sun.star.text.TextContentAnchorType import AT_PAGE

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Page one", False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    cur.setPropertyValue("BreakType", PAGE_BEFORE)
    text.insertString(cur, "Page two", False)

    controller = doc.getCurrentController()
    vc = controller.getViewCursor()
    vc.gotoRange(cur.getEnd(), False)
    assert vc.getPage() == 2

    tctx = native_writer_tool_context(doc, ctx)
    first = EmbeddedInsert().execute(
        tctx, object_type="spreadsheet", target="selection", name="Page2AtPage", data=[["a"]]
    )
    assert first["status"] == "ok", first
    emb = doc.getEmbeddedObjects().getByName("Page2AtPage")
    emb.setPropertyValue("AnchorType", AT_PAGE)
    emb.setPropertyValue("AnchorPageNo", 2)
    assert emb.getAnchor() is None
    controller.select(emb)

    second = EmbeddedInsert().execute(
        tctx, object_type="spreadsheet", target="selection", name="AfterAtPage", data=[["b"]]
    )
    assert second["status"] == "ok", second
    emb2 = doc.getEmbeddedObjects().getByName("AfterAtPage")
    vc.gotoRange(emb2.getAnchor().getStart(), False)
    assert vc.getPage() == 2
