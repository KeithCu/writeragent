"""Unit tests for Writer outline tools."""

import unittest
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.writer.outline import GetDocumentTree


class TestGetDocumentTreeMergedStats(unittest.TestCase):
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

        self.assertEqual(result["stats"], mock_collect.return_value)
        self.assertIn("children", result)
        mock_collect.assert_called_once_with(ctx.doc, ctx.services.document)
        tree_svc.get_document_tree.assert_called_once()


if __name__ == "__main__":
    unittest.main()
