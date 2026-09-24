"""Smoke tests for writer tools: registry has expected tools and schemas are valid."""

import sys
from unittest.mock import patch

from plugin.tests.testing_utils import WriterDocStub

# locale tests replace com.sun.star.lang with an empty module before this file
# is imported. plugin.main imports XInitialization at module level, so put the
# names back on whatever module is installed or collection dies.
_lang = sys.modules.get("com.sun.star.lang")
if _lang is not None:
    class _XInitialization:
        pass

    class _XServiceInfo:
        pass

    if not hasattr(_lang, "XInitialization"):
        _lang.XInitialization = _XInitialization
    if not hasattr(_lang, "XServiceInfo"):
        _lang.XServiceInfo = _XServiceInfo

from plugin.main import get_tools


class TestWriterToolsSmoke:
    def setup_method(self):
        # After earlier tests load real pyuno, bootstrap's get_desktop() can segfault off-LO.
        self._desktop_patch = patch("plugin.framework.uno_context.get_desktop", return_value=None)
        self._desktop_patch.start()

    def teardown_method(self):
        self._desktop_patch.stop()

    def test_registration(self):
        registry = get_tools()
        doc = WriterDocStub()
        writer_tools = {t.name for t in registry.get_tools(doc=doc)}
        # Core / navigation
        assert ("get_document_tree") in (writer_tools)
        assert ("add_comment") in (writer_tools)
        assert ("get_document_stats") not in (writer_tools)
        assert ("get_index_stats") not in (writer_tools)
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
            assert (name) not in (writer_tools)
        # Removed tools no longer present
        assert ("get_document_outline") not in (writer_tools)
        assert ("get_heading_content") not in (writer_tools)
        assert ("set_paragraph_text") not in (writer_tools)
        assert ("set_paragraph_style") not in (writer_tools)
        assert ("scan_tasks") not in (writer_tools)
        assert ("get_workflow_status") not in (writer_tools)
        assert ("set_workflow_status") not in (writer_tools)
        assert ("check_stop_conditions") not in (writer_tools)
        assert ("track_changes_comment_insert") not in (writer_tools)
        assert ("track_changes_comment_list") not in (writer_tools)
        assert ("track_changes_comment_delete") not in (writer_tools)
        # Specialized tools are not in the default chat tool list
        assert ("nav_heading") not in (writer_tools)
        assert ("comment_workflow") not in (writer_tools)
        assert ("shape_list_images") not in (writer_tools)
        assert ("delete_shape") not in (writer_tools)

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
            assert (name) in (names), f"expected comments tool {name!r}"
        assert ("comment_workflow") not in (names)
        for leftover in (
            "track_changes_comment_insert",
            "track_changes_comment_list",
            "track_changes_comment_delete",
        ):
            assert (leftover) not in (names)

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
            assert (name) in (names), f"expected tracking tool {name!r}"
        for leftover in (
            "track_changes_comment_insert",
            "track_changes_comment_list",
            "track_changes_comment_delete",
        ):
            assert (leftover) not in (names)

    def test_shapes_domain_domain_verb_names(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="shapes", exclude_tiers=())}
        for name in ("shape_upsert", "shape_delete", "shape_summary", "shape_connect", "shape_group"):
            assert (name) in (names), f"expected shapes tool {name!r}"
        assert ("shape_list_images") not in (names)
        assert ("delete_shape") not in (names)
        assert ("get_draw_summary") not in (names)
        assert ("shapes_connect") not in (names)
        assert ("shapes_group") not in (names)

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
            assert (name) in (names), f"expected structural tool {name!r}"

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
            assert (name) in (names), f"expected indexes tool {name!r}"
        for name in (
            "bibliography_insert_citation",
            "bibliography_list_citations",
            "bibliography_generate",
        ):
            assert (name) not in (names), f"mock bibliography tool {name!r} must stay unregistered"
        bib_domain = {t.name for t in registry.get_tools(doc=doc, active_domain="bibliography", exclude_tiers=())}
        for name in (
            "bibliography_insert_citation",
            "bibliography_list_citations",
            "bibliography_generate",
        ):
            assert (name) not in (bib_domain)

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
            assert (name) in (names), f"expected mail_merge tool {name!r}"

    def test_schemas(self):
        registry = get_tools()
        doc = WriterDocStub()
        schemas = registry.get_schemas("openai", doc=doc)
        names = {s["function"]["name"] for s in schemas}
        for name in ("get_document_tree", "get_document_content", "search_in_document"):
            assert (name) in (names), f"Schema missing for {name}"
        assert ("get_document_stats") not in (names)
        assert ("get_index_stats") not in (names)
        for s in schemas:
            assert ("description") in (s["function"])
            assert ("parameters") in (s["function"])


