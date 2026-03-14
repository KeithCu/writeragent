
import unittest
from unittest.mock import MagicMock
from plugin.modules.writer.tree import TreeService
from plugin.tests.testing_utils import ElementStub, WriterDocStub

class TestTreeServiceSearch(unittest.TestCase):
    def setUp(self):
        # Setup a document with specific headings
        self.elements = [
            ElementStub("Introduction", outline_level=1), # para 0
            ElementStub("Body Paragraph 1"),
            ElementStub("Installation Guide", outline_level=1), # para 2
            ElementStub("Getting Started", outline_level=2), # para 3
            ElementStub("Advanced Usage", outline_level=2), # para 4
            ElementStub("Conclusion", outline_level=1), # para 5
        ]
        self.doc = WriterDocStub(self.elements)

        self.doc_svc = MagicMock()
        self.doc_svc.doc_key.return_value = "test_doc"
        self.doc_svc.yield_to_gui = MagicMock()

        self.bm_svc = MagicMock()
        self.bm_svc.get_mcp_bookmark_map.return_value = {
            0: "bm_intro",
            2: "bm_install",
            3: "bm_getting",
            4: "bm_advanced",
            5: "bm_conclusion"
        }

        self.events = MagicMock()
        self.tree_svc = TreeService(self.doc_svc, self.bm_svc, self.events)

    def test_exact_match(self):
        # Should match "Introduction" exactly
        res = self.tree_svc._find_heading_by_text(self.doc, "Introduction")
        self.assertIsNotNone(res)
        self.assertEqual(res["text"], "Introduction")
        self.assertEqual(res["para_index"], 0)
        self.assertEqual(res["bookmark"], "bm_intro")

    def test_exact_match_case_insensitive(self):
        res = self.tree_svc._find_heading_by_text(self.doc, "introduction")
        self.assertIsNotNone(res)
        self.assertEqual(res["text"], "Introduction")

    def test_exact_match_with_whitespace(self):
        res = self.tree_svc._find_heading_by_text(self.doc, "  Introduction  ")
        self.assertIsNotNone(res)
        self.assertEqual(res["text"], "Introduction")

    def test_prefix_match(self):
        # "Install" should match "Installation Guide"
        res = self.tree_svc._find_heading_by_text(self.doc, "Install")
        self.assertIsNotNone(res)
        self.assertEqual(res["text"], "Installation Guide")
        self.assertEqual(res["para_index"], 2)

    def test_substring_match(self):
        # "Started" should match "Getting Started"
        res = self.tree_svc._find_heading_by_text(self.doc, "Started")
        self.assertIsNotNone(res)
        self.assertEqual(res["text"], "Getting Started")
        self.assertEqual(res["para_index"], 3)

    def test_priority_exact_over_prefix(self):
        # If we had "Intro" and "Introduction", searching for "Intro" should match "Intro" exactly
        self.elements.insert(0, ElementStub("Intro", outline_level=1))
        # Now "Intro" is para 0, "Introduction" is para 1 (if we rebuild)
        # Re-setup for this specific test
        doc = WriterDocStub(self.elements)
        self.bm_svc.get_mcp_bookmark_map.return_value[0] = "bm_intro_short"

        res = self.tree_svc._find_heading_by_text(doc, "Intro")
        self.assertIsNotNone(res)
        self.assertEqual(res["text"], "Intro")

    def test_priority_prefix_over_substring(self):
        # Search "Advanced" in a doc with "Advanced Usage" and "Super Advanced"
        elements = [
            ElementStub("Super Advanced", outline_level=1), # substring match
            ElementStub("Advanced Usage", outline_level=1), # prefix match
        ]
        doc = WriterDocStub(elements)
        res = self.tree_svc._find_heading_by_text(doc, "Advanced")
        self.assertIsNotNone(res)
        self.assertEqual(res["text"], "Advanced Usage")

    def test_no_match(self):
        res = self.tree_svc._find_heading_by_text(self.doc, "NonExistent")
        self.assertIsNone(res)

    def test_empty_search(self):
        res = self.tree_svc._find_heading_by_text(self.doc, "")
        self.assertIsNone(res)
        res = self.tree_svc._find_heading_by_text(self.doc, "   ")
        self.assertIsNone(res)

if __name__ == "__main__":
    unittest.main()
