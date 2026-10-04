# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""CharPosture italic must be FontSlant.ITALIC, not the integer 1 (OBLIQUE)."""

from com.sun.star.awt.FontSlant import ITALIC, NONE, OBLIQUE

from plugin.doc.visual_helpers import apply_character_properties
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("writer")
def test_apply_character_properties_italic_uses_font_slant_italic(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    text.insertString(cursor, "Hello", False)
    cursor.gotoStart(False)
    cursor.gotoEnd(True)

    results = apply_character_properties(cursor, italic=True)
    assert results.get("CharPosture") is True
    posture = cursor.getPropertyValue("CharPosture")
    assert posture == ITALIC
    assert posture != OBLIQUE

    results = apply_character_properties(cursor, italic=False)
    assert results.get("CharPosture") is True
    assert cursor.getPropertyValue("CharPosture") == NONE
