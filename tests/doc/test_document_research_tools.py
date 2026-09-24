# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for document research MCP helpers (no LibreOffice required)."""

from plugin.doc.document_research_tools import ListOpenDocuments


def test_list_open_documents_does_not_require_a_document():
    # Live finding (2026-06-28): the MCP no-document gate blocks tools whose `requires_document`
    # is the default True. list_open_documents must work with no document open.
    assert ListOpenDocuments.requires_document is False
