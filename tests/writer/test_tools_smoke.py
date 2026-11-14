"""Smoke tests for writer tools: registry has expected tools and schemas are valid."""

from unittest.mock import patch


from plugin.tests.testing_utils import WriterDocStub, setup_uno_mocks

# Locale tests swap com.sun.star.* for empty modules during collection.
# The old grab-bag modules called setup_uno_mocks() at import, which put
# NoSuchElementException / XInitialization back before later imports.
setup_uno_mocks()

from plugin.app import get_tools


class TestWriterToolsSmoke:
    def setup_method(self):
        # After earlier tests load real pyuno, bootstrap's get_desktop() can segfault off-LO.
        self._desktop_patch = patch("plugin.framework.uno_context.get_desktop", return_value=None)
        self._desktop_patch.start()
        # The review registry's frame sweep binds get_desktop at import, so it is patched there too.
        self._rva_desktop_patch = patch("plugin.writer.review_view_attachment.get_desktop", return_value=None)
        self._rva_desktop_patch.start()

    def teardown_method(self):
        self._rva_desktop_patch.stop()
        self._desktop_patch.stop()

    def test_registration(self):
        registry = get_tools()
        doc = WriterDocStub()
        writer_tools = {t.name for t in registry.get_tools(doc=doc)}
        # Core / navigation
        assert ("get_document_tree") in (writer_tools)
        assert ("add_comment") in (writer_tools)
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
        # Specialized tools are not in the default chat tool list
        assert ("nav_heading") not in (writer_tools)

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
            "comment_edit",
        ):
            assert (name) in (names), f"expected comments tool {name!r}"

    def test_tracking_domain_is_redlines_only(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="tracking", exclude_tiers=())}
        for name in (
            "tracking_start",
            "tracking_stop",
            "tracking_list",
            "tracking_show",
            "tracking_manage",
        ):
            assert (name) in (names), f"expected tracking tool {name!r}"

    def test_shapes_domain_domain_verb_names(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="shapes", exclude_tiers=())}
        for name in ("shape_upsert", "shape_delete", "shape_summary", "shape_connect", "shape_group"):
            assert (name) in (names), f"expected shapes tool {name!r}"

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
            "get_page_objects",
        ):
            assert (name) in (names), f"expected structural tool {name!r}"
        table_names = {t.name for t in registry.get_tools(doc=doc, active_domain="tables")}
        assert "get_page_objects" not in table_names

    def test_indexes_domain_bibliography_overload(self):
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="indexes", exclude_tiers=())}
        for name in (
            "indexes_add_mark",
            "indexes_create",
            "indexes_list",
            "indexes_list_toc_entries",
            "indexes_list_cites",
            "indexes_update_all",
            "indexes_refresh_toc_entry",
            "indexes_insert_toc_entry",
            "indexes_delete_toc_entry",
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

    def test_page_domain_reuses_style_list_and_style_clone(self):
        """Page sub-agent can list page styles and clone one; the rest of styles stays hidden."""
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="page", exclude_tiers=())}
        assert ("style_list") in (names)
        assert ("style_clone") in (names)
        for leftover in ("style_get_info", "style_update", "style_create", "style_import"):
            assert (leftover) not in (names), f"page domain must not pull {leftover!r}"
        default_names = {t.name for t in registry.get_tools(doc=doc)}
        assert ("style_list") not in (default_names)
        assert ("style_clone") not in (default_names)

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
        for s in schemas:
            assert ("description") in (s["function"])
            assert ("parameters") in (s["function"])


