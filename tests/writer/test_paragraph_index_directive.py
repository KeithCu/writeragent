# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Coverage test for the 'never cite paragraph numbers' directive (#1 / E4).

Every model-facing tool that returns a para_index / paragraph_index to the model must tell it not to
cite that number to the user (the index is internal and shifts as the document changes). This pins
that coverage so a future tool can't quietly start leaking an index without the directive.
"""

from plugin.framework.prompts import PARAGRAPH_INDEX_DIRECTIVE
from plugin.writer.navigation import NavHeading, NavHeadingChildren, NavSurroundings
from plugin.writer.structural import GetPageObjects


def test_index_tools_embed_central_paragraph_index_directive():
    for tool_cls in (GetPageObjects, NavHeading, NavHeadingChildren, NavSurroundings):
        assert PARAGRAPH_INDEX_DIRECTIVE in (tool_cls.description or ""), tool_cls.name
