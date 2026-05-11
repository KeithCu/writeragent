"""Smoke tests for writer tools: registry has expected tools and schemas are valid."""

import unittest

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.main import get_tools


class _WriterDocStub:
    """Minimal UNO-like doc for registry filtering (supportsService)."""

    def supportsService(self, svc):
        return svc == "com.sun.star.text.TextDocument"


class TestWriterToolsSmoke(unittest.TestCase):
    def test_registration(self):
        registry = get_tools()
        doc = _WriterDocStub()
        writer_tools = {t.name for t in registry.get_tools(doc=doc)}
        # Core / navigation
        self.assertIn("get_document_tree", writer_tools)
        # Content (paragraph batch tools disabled via ToolBaseDummy)
        self.assertIn("get_document_stats", writer_tools)
        for name in (
            "read_paragraphs",
            "insert_at_paragraph",
            "modify_paragraph",
            "delete_paragraph",
            "duplicate_paragraph",
            "clone_heading_block",
            "insert_paragraphs_batch",
        ):
            self.assertNotIn(name, writer_tools)
        # Removed tools no longer present
        self.assertNotIn("get_document_outline", writer_tools)
        self.assertNotIn("get_heading_content", writer_tools)
        self.assertNotIn("set_paragraph_text", writer_tools)
        self.assertNotIn("set_paragraph_style", writer_tools)
        self.assertNotIn("scan_tasks", writer_tools)
        self.assertNotIn("get_workflow_status", writer_tools)
        self.assertNotIn("set_workflow_status", writer_tools)
        self.assertNotIn("check_stop_conditions", writer_tools)
        # Specialized tools are not in the default chat tool list
        self.assertNotIn("navigate_heading", writer_tools)

    def test_structural_domain_includes_navigation_tools(self):
        registry = get_tools()
        doc = _WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="structural")}
        for name in (
            "navigate_heading",
            "get_surroundings",
            "list_sections",
            "goto_page",
            "read_section",
            "get_heading_children",
        ):
            self.assertIn(name, names, f"expected structural tool {name!r}")

    def test_schemas(self):
        registry = get_tools()
        doc = _WriterDocStub()
        schemas = registry.get_schemas("openai", doc=doc)
        names = {s["function"]["name"] for s in schemas}
        for name in ("get_document_tree", "get_document_content", "get_document_stats"):
            self.assertIn(name, names, f"Schema missing for {name}")
        for s in schemas:
            self.assertIn("description", s["function"])
            self.assertIn("parameters", s["function"])


if __name__ == "__main__":
    unittest.main()
