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
# NOTE: SearchInDocument no longer belongs here — its results carry {text, location, context}
# (no paragraph_index anymore) and its description instructs quoting the match text + location
# instead of any internal index. Pinned separately below so the intent can't silently regress.
_INDEX_TOOLS = (GetPageObjects, GetDocumentTree, GetHeadingChildren)


def test_index_tools_warn_against_citing_paragraph_numbers():
    for tool_cls in _INDEX_TOOLS:
        desc = (tool_cls.description or "").lower()
        assert "cite paragraph numbers" in desc, "%s leaks an index without the directive" % tool_cls.name


def test_get_page_objects_uses_the_central_directive_constant():
    # The previously-uncovered tool now carries the canonical constant verbatim (single source).
    assert PARAGRAPH_INDEX_DIRECTIVE in GetPageObjects.description


def test_search_reports_locations_instead_of_internal_indexes():
    # Search's replacement for the directive: no index in results, and the description steers the
    # model to quote match text + location.
    desc = (SearchInDocument.description or "").lower()
    assert "location" in desc
    assert "internal index" in desc
    assert "paragraph_index" not in desc
