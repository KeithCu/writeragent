# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert editable LibreOffice Math objects into Writer text."""

from __future__ import annotations

from typing import Any

# Same CLSID as documented in LibreOffice programming guides (FormulaDocument).
MATH_CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0

# com.sun.star.text.TextContentAnchorType.AS_CHARACTER (inline / in-flow)
_ANCHOR_AS_CHARACTER = 1


def insert_writer_math_formula(
    model: Any,
    cursor: Any,
    starmath: str,
    *,
    display_block: bool,
) -> None:
    """Insert a Writer formula object at *cursor* and leave the cursor after it.

    *display_block*: surround the object with paragraph breaks so it occupies
    its own paragraph (display-style math).
    """
    text = model.getText()
    if display_block:
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)

    embed = model.createInstance("com.sun.star.text.TextEmbeddedObject")
    embed.CLSID = MATH_CLSID
    embed.AnchorType = _ANCHOR_AS_CHARACTER
    text.insertTextContent(cursor, embed, False)

    inner = embed.getEmbeddedObject()
    inner.Formula = starmath

    if display_block:
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
