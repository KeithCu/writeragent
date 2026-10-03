# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Paragraph range lookup must survive a table in the text enumeration."""

from plugin.doc.paragraph_search import find_paragraph_for_range, get_paragraph_ranges
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0


@native_test
@with_native_doc("writer")
def test_find_paragraph_after_table_keeps_index(ctx, doc):
    """A table between paragraphs is not an XTextRange.

    What was wrong: createEnumeration yields the table, and find_paragraph_for_range
    called getStart on it. That raised AttributeError, so the paragraph after the
    table could not be resolved. The enumeration index of that paragraph (tables
    occupy a slot) must still come back.
    """
    text = doc.getText()
    text.setString("")
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    text.insertString(cursor, "Before table", False)
    text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)

    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cursor, table, False)
    table.getCellByName("A1").setString("CELL")

    cursor.gotoEnd(False)
    text.insertString(cursor, "After table", False)

    ranges = get_paragraph_ranges(doc)
    assert len(ranges) == 3
    assert ranges[1].supportsService("com.sun.star.text.TextTable")
    assert not hasattr(ranges[1], "getStart")

    after = ranges[2]
    assert after.getString() == "After table"
    assert find_paragraph_for_range(after, ranges, text) == 2

    # The table anchor sits in the gap between the paragraphs, not inside either.
    assert find_paragraph_for_range(table.getAnchor(), ranges, text) == 1
