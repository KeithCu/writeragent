"""Unit tests for Writer outline tools."""

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import DocumentDisposedError
import plugin.writer.outline as outline
from plugin.writer.outline import GetDocumentTree, collect_document_stats


def test_model_facing_docstring_uses_strategy_parameter():
    doc = outline.__doc__ or ""
    assert 'strategy="heading_only"' in doc
    assert "content_strategy" not in doc


class TestGetDocumentTreeMergedStats:
    @patch("plugin.writer.outline.collect_document_stats")
    def test_execute_includes_stats(self, mock_collect):
        mock_collect.return_value = {
            "character_count": 100,
            "word_count": 20,
            "paragraph_count": 5,
            "page_count": 2,
            "heading_count": 3,
        }
        tree_svc = MagicMock()
        tree_svc.get_document_tree.return_value = {
            "status": "ok",
            "children": [],
            "total_paragraphs": 5,
            "page_count": 2,
        }
        ctx = MagicMock()
        ctx.doc = MagicMock()
        ctx.services.writer_tree = tree_svc
        ctx.services.document = MagicMock()

        tool = GetDocumentTree()
        result = tool.execute(ctx)

        assert (result["stats"]) == (mock_collect.return_value)
        assert ("children") in (result)
        mock_collect.assert_called_once_with(ctx.doc, ctx.services.document)
        tree_svc.get_document_tree.assert_called_once()


def test_collect_document_stats_disposal_is_not_zero_headings():
    doc = MagicMock()
    doc.getText.return_value.createTextCursor.return_value.getString.return_value = ""
    doc_svc = MagicMock()
    doc_svc.get_paragraph_ranges.return_value = []
    doc_svc.get_page_count.return_value = 1
    with patch(
        "plugin.writer.outline.build_heading_tree",
        side_effect=DocumentDisposedError("closed", object_type="Document"),
    ):
        with pytest.raises(DocumentDisposedError):
            collect_document_stats(doc, doc_svc)


