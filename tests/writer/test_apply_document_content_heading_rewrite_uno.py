# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Regression test: replacing text inside a heading paragraph with inline HTML (e.g. a
# <span>) used to silently downgrade the paragraph to a normal one, losing the heading
# level (and its outline semantics). The fix preserves the target paragraph style for
# inline-only content and inserts the fragment raw, so the StarWriter HTML filter does
# not wrap it in an extra <p>.
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
def test_apply_document_content_preserves_heading_level_uno():
    """Replacing text inside a Heading 3 (with content that has a <span>) must not
    demote the paragraph to Standard."""
    doc = _test_doc
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "4.1.1 Engine selection", False)

    # make the paragraph a Heading 3
    pcur = text.createTextCursorByRange(text.getStart())
    pcur.gotoEnd(True)
    pcur.setPropertyValue("ParaStyleName", "Heading 3")
    chk0 = text.createTextCursorByRange(text.getStart())
    assert chk0.getPropertyValue("ParaStyleName") == "Heading 3"

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    res = ApplyDocumentContent().execute(
        tool_ctx,
        content=['<span style="background: transparent">4.1.1 Engine selection</span>'],
        old_content="4.1.1 Engine selection",
        target="search",
    )
    assert res.get("status") == "ok", f"apply_document_content failed: {res}"

    # EXPECTED: the paragraph is still Heading 3
    chk = text.createTextCursorByRange(text.getStart())
    para_style = chk.getPropertyValue("ParaStyleName")
    assert para_style == "Heading 3", \
        f"apply_document_content demoted the heading to '{para_style}'"
