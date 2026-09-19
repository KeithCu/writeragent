# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Regression test: apply_document_content(target='search') editing text inside a
# table cell. replace_preserving_format built its cursor on the document body text
# (model.getText()) instead of the matched range's own XText (the cell), raising the
# UNO RuntimeException "End of content node doesn't have the proper start node" and
# leaving the cell uneditable. The fix uses target_range.getText(), so the cursor
# resolves to the cell.
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.writer.content import ApplyDocumentContent
from plugin.tests.testing_utils import (
    TestingFactory,
    skip_windows_leftover_hidden_load,
    with_native_doc,
)


@native_test
@with_native_doc("writer")
def test_apply_document_content_edits_table_cell_uno(ctx, doc):
    """Editing a cell's text via target='search' should work; it used to raise a
    cursor RuntimeException (body XText vs the cell's XText)."""
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(3, 2)
    text.insertTextContent(text.createTextCursor(), tbl, False)
    tbl.getCellByName("A2").setString("MinerU")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    # plain-text content -> format-preserving path (where the bug lived).
    res = ApplyDocumentContent().execute(
        tool_ctx, content=["MinerU-EDIT"], old_content="MinerU", target="search"
    )
    assert res.get("status") == "ok", f"expected to edit the cell; got {res}"
    assert "MinerU-EDIT" in tbl.getCellByName("A2").getString()


@native_test
@with_native_doc("writer")
def test_apply_document_content_refuses_host_cell_with_nested_table_uno(ctx, doc):
    """Rewriting a host cell via apply_document_content would wipe the nested table."""
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    host = outer.getCellByName("B2")
    host.setString("HOST_CAPTION")
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    host.insertTextContent(host.getEnd(), nested, False)
    nested.setName("ApplyNested")
    nested.getCellByName("A1").setString("KEEP_INNER")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(
        tool_ctx, content=["WIPED"], old_content="HOST_CAPTION", target="search"
    )
    assert res.get("status") == "error", res
    assert "table_set_cell" in (res.get("message") or ""), res
    assert doc.getTextTables().hasByName("ApplyNested")
    assert nested.getCellByName("A1").getString() == "KEEP_INNER"
