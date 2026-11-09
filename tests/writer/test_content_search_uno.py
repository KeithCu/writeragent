# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Integration tests for native regex-based and chaining-based content searches.
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.writer.search import find_first_range, find_all_ranges
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("writer")
def test_search_multi_paragraph_body_uno(ctx, doc):
    """Verify that multi-paragraph search succeeds using chaining in the body."""
    text = doc.getText()
    cursor = text.createTextCursor()

    text.insertString(cursor, "First paragraph of the test.\nSecond paragraph of the test.", False)

    found = find_first_range(doc, "First paragraph of the test.\nSecond paragraph of the test.")
    assert found is not None
    assert "First paragraph" in found.getString()
    assert "Second paragraph" in found.getString()


@native_test
@with_native_doc("writer")
def test_search_exotic_space_in_cell_uno(ctx, doc):
    """Verify that search finds exotic space matches inside a table cell."""
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(text.createTextCursor(), tbl, False)

    cell = tbl.getCellByName("A1")
    cell.setString("Hello\u00a0World")

    found = find_first_range(doc, "Hello World")
    assert found is not None
    assert found.getString() == "Hello\u00a0World"


@native_test
@with_native_doc("writer")
def test_search_quotes_and_dashes_uno(ctx, doc):
    """Verify that lenient search equates straight and curly quotes and dash variants."""
    text = doc.getText()
    cursor = text.createTextCursor()
    text.insertString(cursor, "It’s a–b", False)

    found = find_first_range(doc, "It's a-b")
    assert found is not None
    assert found.getString() == "It’s a–b"


@native_test
@with_native_doc("writer")
def test_search_multi_paragraph_in_frame_uno(ctx, doc):
    """Verify that search finds multi-paragraph matches inside a text frame."""
    text = doc.getText()
    frame = doc.createInstance("com.sun.star.text.TextFrame")
    text.insertTextContent(text.createTextCursor(), frame, False)

    frame_text = frame.getText()
    fc = frame_text.createTextCursor()
    frame_text.insertString(fc, "Inside Frame Para 1.\nInside Frame Para 2.", False)

    found = find_first_range(doc, "Inside Frame Para 1.\nInside Frame Para 2.")
    assert found is not None
    assert "Para 1" in found.getString()
    assert "Para 2" in found.getString()


@native_test
@with_native_doc("writer")
def test_search_real_paragraph_break_body_uno(ctx, doc):
    """Multi-paragraph chaining with real paragraph breaks (multiple XText paragraphs)."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "First Paragraph (Real).", False)
    text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK
    text.insertString(cursor, "Second Paragraph (Real).", False)

    found = find_first_range(doc, "First Paragraph (Real).\nSecond Paragraph (Real).")
    assert found is not None
    assert "First Paragraph (Real)" in found.getString()
    assert "Second Paragraph (Real)" in found.getString()


@native_test
@with_native_doc("writer")
def test_search_newline_collapsed_artifact_uno(ctx, doc):
    """HTML wrap artifact: old_content has \\n but document has a normal space on one line."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, "foo bar", False)

    found = find_first_range(doc, "foo\nbar")
    assert found is not None
    assert found.getString() == "foo bar"


@native_test
@with_native_doc("writer")
def test_search_case_insensitive_uno(ctx, doc):
    """LO regex case-insensitive pass matches mixed-case document text."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    needle = "UNIQUE_CI_HELLO world"
    text.insertString(cursor, needle, False)

    found = find_first_range(doc, "unique_ci_hello world")
    assert found is not None, "case-insensitive search should find mixed-case text"
    assert found.getString().lower() == needle.lower(), (
        "expected %r, got %r" % (needle, found.getString())
    )


@native_test
@with_native_doc("writer")
def test_search_middle_anchor_chaining_uno(ctx, doc):
    """Chaining with anchor on a middle paragraph (backward + forward verification)."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    text.insertString(cursor, "Alpha line.", False)
    text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK
    text.insertString(cursor, "Middle anchor text.", False)
    text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK
    text.insertString(cursor, "Omega line.", False)

    found = find_first_range(doc, "Alpha line.\nMiddle anchor text.\nOmega line.")
    assert found is not None
    assert "Alpha line" in found.getString()
    assert "Middle anchor" in found.getString()
    assert "Omega line" in found.getString()


@native_test
@with_native_doc("writer")
def test_search_all_matches_uno(ctx, doc):
    """all_matches returns every LO regex hit in document order."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, "needle here. Another needle there.", False)

    ranges = find_all_ranges(doc, "needle")
    assert len(ranges) == 2
    texts = [r.getString() for r in ranges]
    assert texts == ["needle", "needle"]


@native_test
@with_native_doc("writer")
def test_search_all_matches_multi_paragraph_chaining_uno(ctx, doc):
    """all_matches with paragraph chaining finds multiple cross-paragraph occurrences."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    for _ in range(2):
        text.insertString(cursor, "Block start.", False)
        text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK
        text.insertString(cursor, "Block end.", False)
        text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK

    ranges = find_all_ranges(doc, "Block start.\nBlock end.")
    assert len(ranges) == 2
    for r in ranges:
        assert "Block start" in r.getString()
        assert "Block end" in r.getString()


@native_test
@with_native_doc("writer")
def test_search_replace_long_needle_in_table_cell_uno(ctx, doc):
    """A long needle inside a table cell must not be blocked by the body oversized check."""
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.tests.testing_utils import native_writer_tool_context

    text = doc.getText()
    cursor = text.createTextCursor()
    # A short body paragraph of ~90 chars above the table.
    text.insertString(cursor, "Short document header paragraph describing the contents below for reference purposes only.\n", False)

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(text.createTextCursor(), tbl, False)

    long_cell_text = (
        "This is a relatively long sentence sitting inside a table cell that exceeds "
        "one hundred characters in length and should be safely replaced."
    )
    assert len(long_cell_text) >= 120

    cell = tbl.getCellByName("A1")
    cell.setString(long_cell_text)

    tool_ctx = native_writer_tool_context(doc, ctx)
    res = ApplyDocumentContent().execute(
        tool_ctx,
        target="search",
        old_content=long_cell_text,
        content=["Replacement cell sentence."],
    )
    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1
    assert "Replacement cell sentence." in cell.getString()
