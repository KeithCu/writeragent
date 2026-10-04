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

from unittest.mock import MagicMock, patch
from plugin.doc.document_research_tools import ListNearbyFiles

@patch("plugin.doc.document_research_tools.list_nearby_files")
def test_list_nearby_files_stop_checker(mock_list):
    mock_list.side_effect = InterruptedError()

    tool = ListNearbyFiles()
    ctx = MagicMock()
    ctx.stop_checker = MagicMock(return_value=True)

    result = tool.execute(ctx)

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"
