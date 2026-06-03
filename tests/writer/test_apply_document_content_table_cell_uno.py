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

from plugin.testing_runner import native_test, setup, teardown
from plugin.writer.content import ApplyDocumentContent
from plugin.tests.testing_utils import TestingFactory

_test_doc = None
_test_ctx = None


@setup
def my_setup(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    _test_doc = TestingFactory.create_native_doc(ctx, doc_type="writer")


@teardown
def my_teardown(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None


@native_test
def test_apply_document_content_edits_table_cell_uno():
    """Editing a cell's text via target='search' should work; it used to raise a
    cursor RuntimeException (body XText vs the cell's XText)."""
    doc = _test_doc
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(3, 2)
    text.insertTextContent(text.createTextCursor(), tbl, False)
    tbl.getCellByName("A2").setString("MinerU")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    # plain-text content -> format-preserving path (where the bug lived).
    res = ApplyDocumentContent().execute(
        tool_ctx, content=["MinerU-EDIT"], old_content="MinerU", target="search"
    )
    assert res.get("status") == "ok", f"expected to edit the cell; got {res}"
    assert "MinerU-EDIT" in tbl.getCellByName("A2").getString()
