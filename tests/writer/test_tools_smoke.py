"""Smoke tests for writer tools: registry has expected tools and schemas are valid."""

import unittest
from unittest.mock import patch

from plugin.tests.testing_utils import WriterDocStub, setup_uno_mocks
setup_uno_mocks()

from plugin.main import get_tools


class TestWriterToolsSmoke(unittest.TestCase):
    def setUp(self):
        # After earlier tests load real pyuno, bootstrap's get_desktop() can segfault off-LO.
        self._desktop_patch = patch("plugin.framework.uno_context.get_desktop", return_value=None)
        self._desktop_patch.start()

    def tearDown(self):
        self._desktop_patch.stop()

    def test_registration(self):
        registry = get_tools()
        doc = WriterDocStub()
        writer_tools = {t.name for t in registry.get_tools(doc=doc)}
        # Core / navigation
        self.assertIn("get_document_tree", writer_tools)
        self.assertIn("add_comment", writer_tools)
        self.assertNotIn("get_document_stats", writer_tools)
        self.assertNotIn("get_index_stats", writer_tools)
        # Content (paragraph batch tools disabled via ToolBaseDummy)
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
        self.assertNotIn("track_changes_comment_insert", writer_tools)
        self.assertNotIn("track_changes_comment_list", writer_tools)
        self.assertNotIn("track_changes_comment_delete", writer_tools)
        # Specialized tools are not in the default chat tool list
        self.assertNotIn("nav_heading", writer_tools)
        self.assertNotIn("comment_workflow", writer_tools)
        self.assertNotIn("shape_list_images", writer_tools)
        self.assertNotIn("delete_shape", writer_tools)

    def test_comments_domain_skinny_workflow_tools(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="comments", exclude_tiers=())}
        for name in (
            "comment_scan_tasks",
            "comment_workflow_get",
            "comment_workflow_set",
            "comment_check_stop",
            "comment_list",
        ):
            self.assertIn(name, names, f"expected comments tool {name!r}")
        self.assertNotIn("comment_workflow", names)
        for leftover in (
            "track_changes_comment_insert",
            "track_changes_comment_list",
            "track_changes_comment_delete",
        ):
            self.assertNotIn(leftover, names)

    def test_tracking_domain_is_redlines_only(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="tracking", exclude_tiers=())}
        for name in (
            "track_changes_start",
            "track_changes_stop",
            "track_changes_list",
            "track_changes_show",
            "manage_tracked_changes",
        ):
            self.assertIn(name, names, f"expected tracking tool {name!r}")
        for leftover in (
            "track_changes_comment_insert",
            "track_changes_comment_list",
            "track_changes_comment_delete",
        ):
            self.assertNotIn(leftover, names)

    def test_shapes_domain_domain_verb_names(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="shapes", exclude_tiers=())}
        for name in ("shape_upsert", "shape_delete", "shape_summary", "shape_connect", "shape_group"):
            self.assertIn(name, names, f"expected shapes tool {name!r}")
        self.assertNotIn("shape_list_images", names)
        self.assertNotIn("delete_shape", names)
        self.assertNotIn("get_draw_summary", names)
        self.assertNotIn("shapes_connect", names)
        self.assertNotIn("shapes_group", names)

    def test_structural_domain_includes_navigation_tools(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="structural")}
        for name in (
            "nav_heading",
            "nav_surroundings",
            "section_list",
            "nav_goto_page",
            "section_read",
            "nav_heading_children",
        ):
            self.assertIn(name, names, f"expected structural tool {name!r}")

    def test_indexes_domain_bibliography_overload(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="indexes", exclude_tiers=())}
        for name in (
            "indexes_add_mark",
            "indexes_create",
            "indexes_list",
            "indexes_list_cites",
            "indexes_update_all",
            "indexes_refresh_toc_entry",
        ):
            self.assertIn(name, names, f"expected indexes tool {name!r}")
        for name in (
            "bibliography_insert_citation",
            "bibliography_list_citations",
            "bibliography_generate",
        ):
            self.assertNotIn(name, names, f"mock bibliography tool {name!r} must stay unregistered")
        bib_domain = {t.name for t in registry.get_tools(doc=doc, active_domain="bibliography", exclude_tiers=())}
        for name in (
            "bibliography_insert_citation",
            "bibliography_list_citations",
            "bibliography_generate",
        ):
            self.assertNotIn(name, bib_domain)

    def test_mail_merge_domain_tools(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="mail_merge", exclude_tiers=())}
        for name in (
            "mail_merge_list_sources",
            "mail_merge_register_source",
            "mail_merge_insert_field",
            "mail_merge_list_fields",
            "mail_merge_run",
        ):
            self.assertIn(name, names, f"expected mail_merge tool {name!r}")

    def test_schemas(self):
        registry = get_tools()
        doc = WriterDocStub()
        schemas = registry.get_schemas("openai", doc=doc)
        names = {s["function"]["name"] for s in schemas}
        for name in ("get_document_tree", "get_document_content", "search_in_document"):
            self.assertIn(name, names, f"Schema missing for {name}")
        self.assertNotIn("get_document_stats", names)
        self.assertNotIn("get_index_stats", names)
        for s in schemas:
            self.assertIn("description", s["function"])
            self.assertIn("parameters", s["function"])


if __name__ == "__main__":
    unittest.main()
