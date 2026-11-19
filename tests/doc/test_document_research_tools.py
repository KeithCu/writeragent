# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for document research MCP helpers (no LibreOffice required)."""

import pytest

from plugin.chatbot.agent_manual import list_topics
from plugin.doc.document_research_tools import GetGuidance, ListOpenDocuments


def test_list_open_documents_does_not_require_a_document():
    # Live finding (2026-06-28): the MCP no-document gate blocks tools whose `requires_document`
    # is the default True. document_list_open must work with no document open.
    assert ListOpenDocuments.requires_document is False


@pytest.mark.parametrize("doc_type", ["calc", "draw"])
def test_manual_read_description_names_no_topic_the_type_lacks(doc_type: str) -> None:
    desc = GetGuidance().get_description(doc_type)
    writer_only = set(list_topics("writer")) - set(list_topics(doc_type))
    assert writer_only
    for topic in writer_only:
        assert topic.replace("-", " ") not in desc
