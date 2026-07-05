# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Comment helper tests (list/read paths). No LibreOffice required."""
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()


def test_read_annotation_falls_back_to_paragraph_context():
    from plugin.writer.specialized.comments import _read_annotation

    anchor = MagicMock()
    anchor.getString.return_value = ""
    field = MagicMock()
    field.getAnchor.return_value = anchor
    field.getPropertyValue.side_effect = lambda p: {"Author": "Rev", "Content": "note"}.get(p, "")
    doc_svc = MagicMock()
    doc_svc.find_paragraph_for_range.return_value = 7
    with patch("plugin.writer.search._enclosing_paragraph_text", return_value="  the clause the comment covers  "):
        entry = _read_annotation(field, [], MagicMock(), doc_svc)
    assert entry["anchor_preview"] == "the clause the comment covers"
    assert entry["anchor_is_paragraph_context"] is True
