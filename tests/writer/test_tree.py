
import unittest
from unittest.mock import MagicMock
from plugin.writer.tree import TreeService
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

        # We need a mock services object
        services = MagicMock()
        services.document = self.doc_svc
        services.writer_bookmarks = self.bm_svc
        services.events = self.events

        self.tree_svc = TreeService(services)

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

    def test_invalidate_by_key_does_not_call_doc_key(self):
        self.tree_svc._tree_cache["uid:1"] = {"text": "stale"}
        self.doc_svc.doc_key.side_effect = RuntimeError("must not touch the model")
        self.tree_svc._on_cache_invalidated(key="uid:1")
        self.assertNotIn("uid:1", self.tree_svc._tree_cache)

    def test_invalidate_doc_none_clears_all(self):
        self.tree_svc._tree_cache["uid:1"] = {"text": "a"}
        self.tree_svc._tree_cache["uid:2"] = {"text": "b"}
        self.tree_svc._on_cache_invalidated(doc=None)
        self.assertEqual(self.tree_svc._tree_cache, {})

    def test_unknown_key_is_not_stored(self):
        from plugin.doc.document_helpers import UNKNOWN_DOC_KEY

        self.doc_svc.doc_key.return_value = UNKNOWN_DOC_KEY
        root = self.tree_svc.build_heading_tree(self.doc)
        self.assertEqual(root["children"][0]["text"], "Introduction")
        self.assertEqual(self.tree_svc._tree_cache, {})

    def test_fingerprint_change_rebuilds_without_invalidate_event(self):
        """GHA 35466498641: Windows modify listener can miss insertString."""
        self.doc.CharacterCount = 10
        first = self.tree_svc.build_heading_tree(self.doc)
        cached = self.tree_svc.build_heading_tree(self.doc)
        self.assertIs(cached, first)
        self.doc.CharacterCount = 16
        second = self.tree_svc.build_heading_tree(self.doc)
        self.assertIsNot(second, first)

    def test_chapter_number_optional_and_locator(self):
        class _Labeled(ElementStub):
            def __init__(self, text, outline_level=1, list_label=""):
                super().__init__(text, outline_level=outline_level)
                self._list_label = list_label

            def getPropertyValue(self, name):
                if name == "ListLabelString":
                    return self._list_label
                return super().getPropertyValue(name)

        from plugin.framework.errors import ToolExecutionError

        elements = [
            _Labeled("DOCUMENT 7", 1, "3"),
            _Labeled("Section Alpha", 2, "3.1."),
            ElementStub("Body paragraph"),
            _Labeled("Bare heading", 1, ""),
        ]
        doc = WriterDocStub(elements)
        root = self.tree_svc.build_heading_tree(doc)
        doc7 = root["children"][0]
        alpha = doc7["children"][0]
        bare = root["children"][1]
        self.assertEqual(doc7["chapter_number"], "3")
        self.assertEqual(doc7["text"], "DOCUMENT 7")
        self.assertEqual(alpha["chapter_number"], "3.1")
        self.assertEqual(alpha["text"], "Section Alpha")
        self.assertNotIn("chapter_number", bare)

        hit = self.tree_svc.resolve_writer_locator(doc, "chapter_number", "3.1")
        self.assertEqual(hit["para_index"], 1)
        ordinal = self.tree_svc.resolve_writer_locator(doc, "heading", "1.1")
        self.assertEqual(ordinal["para_index"], 1)
        with self.assertRaises(ToolExecutionError) as raised:
            self.tree_svc.resolve_writer_locator(doc, "chapter_number", "1.1")
        self.assertIn("chapter_number:1.1", str(raised.exception))
        self.assertIn("sibling-ordinal", str(raised.exception))

if __name__ == "__main__":
    unittest.main()
