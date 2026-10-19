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
        mock_collect.assert_called_once_with(
            ctx.doc,
            ctx.services.document,
            heading_tree=tree_svc.build_heading_tree.return_value,
            paragraph_count=5,
            page_count=2,
        )
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


def test_collect_document_stats_page_failure_keeps_other_counts():
    doc = MagicMock()
    doc_svc = MagicMock()
    doc_svc.get_paragraph_ranges.return_value = [object(), object(), object(), object()]
    doc_svc.get_page_count.side_effect = RuntimeError("layout")
    tree = {
        "children": [
            {"children": []},
            {"children": [{"children": []}]},
        ]
    }
    with (
        patch("plugin.writer.outline.get_string_without_tracked_deletions", return_value="hello world"),
        patch("plugin.writer.outline.build_heading_tree", return_value=tree),
    ):
        stats = collect_document_stats(doc, doc_svc)
    assert stats == {
        "character_count": 11,
        "word_count": 2,
        "paragraph_count": 4,
        "page_count": 0,
        "heading_count": 3,
    }
    doc_svc.get_document_length.assert_not_called()


def test_collect_document_stats_text_failure_uses_length_and_zero_words():
    doc = MagicMock()
    doc_svc = MagicMock()
    doc_svc.get_document_length.return_value = 42
    doc_svc.get_paragraph_ranges.return_value = [object()]
    doc_svc.get_page_count.return_value = 4
    with (
        patch(
            "plugin.writer.outline.get_string_without_tracked_deletions",
            side_effect=RuntimeError("no text"),
        ),
        patch("plugin.writer.outline.build_heading_tree", return_value={"children": []}),
    ):
        stats = collect_document_stats(doc, doc_svc)
    assert stats == {
        "character_count": 42,
        "word_count": 0,
        "paragraph_count": 1,
        "page_count": 4,
        "heading_count": 0,
    }
    doc_svc.get_document_length.assert_called_once_with(doc)
