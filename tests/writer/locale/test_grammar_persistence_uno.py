# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for grammar persistence and language changes."""

from __future__ import annotations

from typing import Any
from plugin.testing_runner import native_test
from tests.testing_utils import with_native_doc


@native_test
@with_native_doc("writer")
def test_apply_language_change_retags_only_matching_paragraph(ctx: Any, doc: Any) -> None:
    """When multiple paragraphs contain the same sentence, only the one matching paragraph_text is retagged."""
    from plugin.writer.locale import grammar_persistence as gp
    from plugin.writer.locale.grammar_worker import apply_language_change

    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        # Paragraph 1
        para1_text = "Before text. This is a duplicate sentence. After text 1."
        text.insertString(cursor, para1_text, False)
        # Paragraph 2
        text.insertControlCharacter(cursor, 0, False)
        para2_text = "Different context. This is a duplicate sentence. After text 2."
        text.insertString(cursor, para2_text, False)

        doc_id = "test-retag-doc"
        gp.grammar_registry.clear_all(ctx)
        dp = gp.get_persistence(ctx, doc_id, model=doc)
        assert dp is not None

        # Apply language change targeting the duplicate sentence in paragraph 2
        success = apply_language_change(
            ctx,
            doc_id,
            "This is a duplicate sentence.",
            "fr-FR",
            start_pos=19,
            paragraph_text=para2_text,
        )
        assert success is True

        enum = text.createEnumeration()
        p1 = enum.nextElement()
        p2 = enum.nextElement()

        p2_cursor = text.createTextCursorByRange(p2.getStart())
        p2_cursor.goRight(19, False)
        p2_cursor.goRight(len("This is a duplicate sentence."), True)
        loc2 = p2_cursor.getPropertyValue("CharLocale")
        assert loc2.Language == "fr" and loc2.Country == "FR"

        p1_cursor = text.createTextCursorByRange(p1.getStart())
        p1_cursor.goRight(13, False)
        p1_cursor.goRight(len("This is a duplicate sentence."), True)
        loc1 = p1_cursor.getPropertyValue("CharLocale")
        assert not (loc1.Language == "fr" and loc1.Country == "FR")
    finally:
        gp.grammar_registry.clear_all(ctx)
