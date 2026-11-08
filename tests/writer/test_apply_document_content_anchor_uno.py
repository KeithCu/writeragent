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
    """Replace a span from start_anchor through end_anchor across styled heading paragraphs."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Heading One", False)
    cursor.gotoStartOfParagraph(True)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    cursor.collapseToEnd()
    text.insertControlCharacter(cursor, 0, False)

    text.insertString(cursor, "MIDDLE-UNIQUE-SHOULD-VANISH", False)
    text.insertControlCharacter(cursor, 0, False)

    text.insertString(cursor, "Heading Two", False)
    cursor.gotoStartOfParagraph(True)
    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    cursor.collapseToEnd()
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
def test_apply_document_content_anchor_range_plain_text_uno(ctx, doc):
    """Plain-text content across paragraphs uses the format-preserving path."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Start Landmark", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Old intermediate content", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "End Landmark", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Keep text after", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Start Landmark",
        end_anchor="End Landmark",
        content="New paragraph one\nNew paragraph two",
    )

    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res
    assert "formatting preserved" in res.get("message", "")
    body = doc.getText().getString()
    assert "New paragraph one" in body
    assert "New paragraph two" in body
    assert "Keep text after" in body
    assert "Old intermediate content" not in body
    assert "Start Landmark" not in body
    assert "End Landmark" not in body


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_identical_strings_uno(ctx, doc):
    """Identical start and end anchor strings match the next occurrence in document order."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Note: first occurrence", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Section middle text", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Note: second occurrence", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Note: third occurrence", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Note:",
        end_anchor="Note:",
        content=["<p>Single consolidated note</p>"],
    )

    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res
    body = doc.getText().getString()
    assert "Single consolidated note" in body
    assert "Note: third occurrence" in body
    assert "first occurrence" not in body
    assert "Section middle text" not in body
    assert "second occurrence" not in body


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_contains_table_refused_uno(ctx, doc):
    """Spans that contain a table between anchors are refused with an actionable error."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Table Section Start", False)
    text.insertControlCharacter(cursor, 0, False)

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(cursor, tbl, False)
    tbl.getCellByName("A1").setString("Cell data")

    cursor.gotoRange(tbl.getAnchor().getEnd(), False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Table Section End", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Table Section Start",
        end_anchor="Table Section End",
        content="New replacement text",
    )

    assert res.get("status") == "error", res
    assert res.get("code") == "TABLE_IN_ANCHOR_RANGE", res
    assert "table_delete" in res.get("message", "")
    assert doc.getTextTables().getCount() == 1


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
