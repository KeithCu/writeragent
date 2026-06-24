# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Coverage test for the 'never cite paragraph numbers' directive (#1 / E4).

Every model-facing tool that returns a para_index / paragraph_index to the model must tell it not to
cite that number to the user (the index is internal and shifts as the document changes). This pins
that coverage so a future tool can't quietly start leaking an index without the directive.
"""
from plugin.framework.constants import PARAGRAPH_INDEX_DIRECTIVE
from plugin.writer.outline import GetDocumentTree, GetHeadingChildren
from plugin.writer.search import SearchInDocument
from plugin.writer.structural import GetPageObjects

# Model-facing tools whose results expose a paragraph index to the model.
_INDEX_TOOLS = (GetPageObjects, GetDocumentTree, GetHeadingChildren, SearchInDocument)


def test_index_tools_warn_against_citing_paragraph_numbers():
    for tool_cls in _INDEX_TOOLS:
        desc = (tool_cls.description or "").lower()
        assert "cite paragraph numbers" in desc, "%s leaks an index without the directive" % tool_cls.name


def test_get_page_objects_uses_the_central_directive_constant():
    # The previously-uncovered tool now carries the canonical constant verbatim (single source).
    assert PARAGRAPH_INDEX_DIRECTIVE in GetPageObjects.description
