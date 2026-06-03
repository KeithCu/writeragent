# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Regression test: apply_document_content(target='search') failed to match document
# text containing a non-breaking space (U+00A0) when old_content used a normal space.
# The fix normalizes NBSP & relatives (U+00A0/202F/2007/2009) to a normal space on
# both the search string and the document text (1:1, so cursor offsets stay aligned).
import uno  # noqa: F401

from plugin.testing_runner import native_test, setup, teardown
from plugin.writer.content import ApplyDocumentContent
from plugin.tests.testing_utils import TestingFactory

_NBSP = chr(0xA0)  # non-breaking space U+00A0 (ASCII-safe in source)

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
def test_apply_document_content_search_matches_nbsp_uno():
    """Searching with a normal space should match text containing a NBSP."""
    doc = _test_doc
    text = doc.getText()
    cur = text.createTextCursor()
    # Document with a NBSP between ')' and 'by' — a common artifact left by a
    # previous replacement (the bug-report scenario).
    text.insertString(cur, "Result figures)" + _NBSP + "by a judge, final.", False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    # old_content uses a NORMAL SPACE; it should match the document's NBSP.
    res = ApplyDocumentContent().execute(
        tool_ctx,
        content=["figures) BY A JUDGE"],
        old_content="figures) by a judge",
        target="search",
    )
    assert res.get("status") == "ok", f"apply_document_content did not match the NBSP: {res}"
    body = doc.getText().getString()
    assert "BY A JUDGE" in body, f"replacement did not happen; text={body!r}"
