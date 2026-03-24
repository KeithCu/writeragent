"""Smoke tests for writer tools: registry has expected tools and schemas are valid."""

import unittest

from plugin.main import get_tools

# Writer table tools (tables.py) are commented out; flip to False after uncommenting
# those ToolBase classes and restoring imports in plugin/modules/writer/__init__.py.
WRITER_TABLE_TOOLS_DISABLED = True


class TestWriterToolsSmoke(unittest.TestCase):
    def test_registration(self):
        registry = get_tools()
        writer_tools = {t.name for t in registry.get_tools(doc_type="writer")}
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

    def test_schemas(self):
        registry = get_tools()
        schemas = registry.get_schemas("openai", doc_type="writer")
        names = {s["function"]["name"] for s in schemas}
        for name in ("get_document_tree", "get_document_content", "get_document_stats"):
            self.assertIn(name, names, f"Schema missing for {name}")
        for s in schemas:
            self.assertIn("description", s["function"])
            self.assertIn("parameters", s["function"])

    @unittest.skipIf(
        WRITER_TABLE_TOOLS_DISABLED,
        "Writer table tools disabled; see plugin/modules/writer/tables.py and __init__.py",
    )
    def test_table_tools_registered(self):
        registry = get_tools()
        names = {t.name for t in registry.get_tools(doc_type="writer")}
        for name in (
            "list_tables",
            "read_table",
            "write_table_cells",
            "create_table",
            "delete_table",
            "set_table_properties",
            "add_table_rows",
            "add_table_columns",
            "delete_table_rows",
            "delete_table_columns",
            "write_table_row",
        ):
            self.assertIn(name, names, f"expected table tool {name!r}")


if __name__ == "__main__":
    unittest.main()
