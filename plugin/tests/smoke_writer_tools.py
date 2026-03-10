"""Smoke tests for writer tools: registry has expected tools and schemas are valid."""

import unittest
from plugin.main import get_tools


class TestWriterToolsSmoke(unittest.TestCase):
    def test_registration(self):
        registry = get_tools()
        writer_tools = {t.name for t in registry.tools_for_doc_type("writer")}
        # Core / navigation
        self.assertIn("get_document_tree", writer_tools)
        self.assertIn("get_heading_children", writer_tools)
        # Content
        self.assertIn("read_paragraphs", writer_tools)
        self.assertIn("insert_at_paragraph", writer_tools)
        self.assertIn("get_document_stats", writer_tools)
        self.assertIn("modify_paragraph", writer_tools)
        # Workflow (merged from scan_tasks, get_workflow_status, set_workflow_status, check_stop_conditions)
        self.assertIn("workflow", writer_tools)
        # Removed tools no longer present
        self.assertNotIn("get_document_outline", writer_tools)
        self.assertNotIn("get_heading_content", writer_tools)
        self.assertNotIn("set_paragraph_text", writer_tools)
        self.assertNotIn("set_paragraph_style", writer_tools)
        self.assertNotIn("scan_tasks", writer_tools)
        self.assertNotIn("get_workflow_status", writer_tools)
        self.assertNotIn("set_workflow_status", writer_tools)
        self.assertNotIn("check_stop_conditions", writer_tools)

    def test_schemas(self):
        registry = get_tools()
        schemas = registry.get_openai_schemas(doc_type="writer")
        names = {s["function"]["name"] for s in schemas}
        for name in ("get_document_tree", "get_heading_children", "read_paragraphs", "get_document_stats", "modify_paragraph", "workflow"):
            self.assertIn(name, names, f"Schema missing for {name}")
        for s in schemas:
            self.assertIn("description", s["function"])
            self.assertIn("parameters", s["function"])


if __name__ == "__main__":
    unittest.main()
