# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Anchor-based range replace tests for apply_document_content."""

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.apply_document_content import ApplyDocumentContent


def _tool_ctx(doc, ctx):
    # GHA 34681661844: avoid windows leftover hidden apply hangs.
    return native_writer_tool_context(
        doc, ctx, skip_reason="apply_document_content Hidden _default swriter"
    )


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_replace_uno(ctx, doc):
    """Replace a span from start_anchor through end_anchor across paragraphs."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Heading One", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "MIDDLE-UNIQUE-SHOULD-VANISH", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Heading Two", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "KEEP-AFTER", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Heading One",
        end_anchor="Heading Two",
        content=["<h2>New heading</h2>", "<p>New body</p>"],
    )

    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res
    body = doc.getText().getString()
    assert "New heading" in body
    assert "New body" in body
    assert "KEEP-AFTER" in body
    assert "MIDDLE-UNIQUE-SHOULD-VANISH" not in body
    assert "Heading One" not in body
    assert "Heading Two" not in body


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_missing_end_uno(ctx, doc):
    """Missing end_anchor returns an error and leaves document text unchanged."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Heading One", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "MIDDLE-UNIQUE-SHOULD-VANISH", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Heading Two", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "KEEP-AFTER", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Heading One",
        end_anchor="No Such Heading",
        content="GONE",
    )

    assert res.get("status") == "error", res
    assert res.get("replaced_count") == 0, res
    msg = res.get("message", "")
    assert "end_anchor" in msg
    assert "not found" in msg
    body = doc.getText().getString()
    assert "MIDDLE-UNIQUE-SHOULD-VANISH" in body
