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
    """Plain-text content across an anchor range is imported as new paragraphs without format preservation."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Start Landmark", False)
    text.insertControlCharacter(cursor, 0, False)
    cursor.setPropertyValue("CharWeight", 150.0)
    text.insertString(cursor, "Old intermediate content", False)
    cursor.setPropertyValue("CharWeight", 100.0)
    text.insertControlCharacter(cursor, 0, False)
    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertString(cursor, "Heading Slice", False)
    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "End Landmark", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Keep text after", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Start Landmark",
        end_anchor="End Landmark",
        content="New paragraph one\n\nNew paragraph two",
    )

    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res
    assert res.get("message") == "Replaced the range from start_anchor through end_anchor."
    assert "formatting preserved" not in res.get("message", "")
    body = doc.getText().getString()
    assert "New paragraph one" in body
    assert "New paragraph two" in body
    assert "Keep text after" in body
    assert "Old intermediate content" not in body
    assert "Heading Slice" not in body
    assert "Start Landmark" not in body
    assert "End Landmark" not in body

    enum = doc.getText().createEnumeration()
    paras_before_keeper = []
    while enum.hasMoreElements():
        para = enum.nextElement()
        if not para.supportsService("com.sun.star.text.Paragraph"):
            continue
        para_str = para.getString().strip()
        if "Keep text after" in para_str:
            break
        paras_before_keeper.append(para)

    non_empty_before_keeper = [p for p in paras_before_keeper if p.getString().strip()]
    assert len(non_empty_before_keeper) == 2
    # HTML import can leave one empty absorb paragraph before the keeper.
    assert len(paras_before_keeper) <= 3
    for p in non_empty_before_keeper:
        assert p.getPropertyValue("ParaStyleName") == "Text body"
        portion_enum = p.createEnumeration()
        while portion_enum.hasMoreElements():
            portion = portion_enum.nextElement()
            assert portion.getPropertyValue("CharWeight") < 150.0


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_mid_paragraph_plain_text_uno(ctx, doc):
    """A mid-paragraph single-line plain anchor replace does not split the host paragraph."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertString(cursor, "The quick brown fox jumps over the lazy dog", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="quick",
        end_anchor="fox",
        content="slow cat",
    )

    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res
    body = doc.getText().getString().strip()
    assert body == "The slow cat jumps over the lazy dog"

    enum = doc.getText().createEnumeration()
    paras = []
    while enum.hasMoreElements():
        para = enum.nextElement()
        if para.supportsService("com.sun.star.text.Paragraph") and para.getString().strip():
            paras.append(para)
    assert len(paras) == 1
    assert paras[0].getPropertyValue("ParaStyleName") == "Heading 2"


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
    text.insertString(cursor, "second occurrence: Note", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "third occurrence: Note", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Note",
        end_anchor="Note",
        content=["<p>Single consolidated note</p>"],
    )

    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res
    body = doc.getText().getString()
    assert "Single consolidated note" in body
    assert "third occurrence: Note" in body
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

    cursor.gotoEnd(False)
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


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_skips_frame_end_and_finds_body_end_uno(ctx, doc):
    """An end anchor inside an attached text frame is skipped in favor of a same-text body end."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Frame Section Start", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Middle Content", False)
    text.insertControlCharacter(cursor, 0, False)

    frame = doc.createInstance("com.sun.star.text.TextFrame")
    text.insertTextContent(cursor, frame, False)
    frame.getText().setString("Target End Landmark")

    cursor.gotoEnd(False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Target End Landmark", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Keep Trailing Text", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Frame Section Start",
        end_anchor="Target End Landmark",
        content="Consolidated Section",
    )

    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res
    body = doc.getText().getString()
    assert "Consolidated Section" in body
    assert "Keep Trailing Text" in body
    assert "Middle Content" not in body
    assert "Frame Section Start" not in body
    assert "Target End Landmark" not in body


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_different_text_refused_uno(ctx, doc):
    """When the only matching end anchor is in a table cell, the different-text error is returned."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Body Start Landmark", False)
    text.insertControlCharacter(cursor, 0, False)

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(cursor, tbl, False)
    tbl.getCellByName("A1").setString("Only End Anchor In Cell")

    cursor.gotoEnd(False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Body Trailing Text", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Body Start Landmark",
        end_anchor="Only End Anchor In Cell",
        content="New Text",
    )

    assert res.get("status") == "error", res
    assert res.get("replaced_count") == 0, res
    msg = res.get("message", "")
    assert "not in the same text" in msg


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_occurrence_uno(ctx, doc):
    """occurrence selects the 0-based matching start anchor landmark."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Section Start", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "First Body", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Section End", False)
    text.insertControlCharacter(cursor, 0, False)

    text.insertString(cursor, "Section Start", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Second Body", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Section End", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="Section Start",
        end_anchor="Section End",
        occurrence=1,
        content="<p>Updated Second Section</p>",
    )

    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1, res
    body = doc.getText().getString()
    assert "First Body" in body
    assert "Updated Second Section" in body
    assert "Second Body" not in body


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_case_sensitive_uno(ctx, doc):
    """case_sensitive=True rejects case mismatch while case_sensitive=False matches it."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "lowercase landmark start", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Intermediate Text", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "lowercase landmark end", False)

    res_strict = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="LOWERCASE LANDMARK START",
        end_anchor="lowercase landmark end",
        case_sensitive=True,
        content="New Text",
    )
    assert res_strict.get("status") == "error", res_strict
    assert "start_anchor not found" in res_strict.get("message", "")

    res_loose = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        start_anchor="LOWERCASE LANDMARK START",
        end_anchor="LOWERCASE LANDMARK END",
        case_sensitive=False,
        content="<p>Replaced Case Insensitive</p>",
    )
    assert res_loose.get("status") == "ok", res_loose
    assert res_loose.get("replaced_count") == 1, res_loose
    body = doc.getText().getString()
    assert "Replaced Case Insensitive" in body
    assert "Intermediate Text" not in body


def _reject_all_changes(doc, ctx):
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = doc.getCurrentController().getFrame()
    helper.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())


@native_test
@with_native_doc("writer")
def test_apply_document_content_anchor_range_review_record_uno(ctx, doc):
    """Anchor range replacement records tracked changes when review mode is record; reject restores."""
    from plugin.framework.config import get_config, save

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Review Start Landmark", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Original Middle Content", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "Review End Landmark", False)

    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    save({flag: "record"})
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(doc, ctx),
            start_anchor="Review Start Landmark",
            end_anchor="Review End Landmark",
            content="Replacement Under Review",
        )
        assert res.get("status") == "ok", res
        assert res.get("replaced_count") == 1, res
        assert doc.getRedlines().getCount() > 0
        body = doc.getText().getString()
        assert "Replacement Under Review" in body

        _reject_all_changes(doc, ctx)
        body_restored = doc.getText().getString()
        assert "Review Start Landmark" in body_restored
        assert "Original Middle Content" in body_restored
        assert "Review End Landmark" in body_restored
        assert "Replacement Under Review" not in body_restored
    finally:
        save({flag: prev})
