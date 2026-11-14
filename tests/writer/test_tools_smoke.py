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
        # Specialized tools are not in the default chat tool list.
        assert "nav_heading" not in writer_tools

    def test_structural_domain_routing_excludes_tables_tools(self):
        registry = get_tools()
        doc = WriterDocStub()
        structural_names = {t.name for t in registry.get_tools(doc=doc, active_domain="structural")}
        table_names = {t.name for t in registry.get_tools(doc=doc, active_domain="tables")}
        assert "get_page_objects" in structural_names
        assert "get_page_objects" not in table_names

    def test_indexes_domain_bibliography_must_stay_unregistered(self):
        registry = get_tools()
        doc = WriterDocStub()
        idx_names = {t.name for t in registry.get_tools(doc=doc, active_domain="indexes", exclude_tiers=())}
        bib_names = {t.name for t in registry.get_tools(doc=doc, active_domain="bibliography", exclude_tiers=())}
        for name in (
            "bibliography_insert_citation",
            "bibliography_list_citations",
            "bibliography_generate",
        ):
            assert name not in idx_names, f"mock bibliography tool {name!r} must stay unregistered"
            assert name not in bib_names

    def test_page_domain_reuses_style_list_and_style_clone(self):
        """Page sub-agent can list page styles and clone one; the rest of styles stays hidden."""
        registry = get_tools()
        doc = WriterDocStub()
        names = {t.name for t in registry.get_tools(doc=doc, active_domain="page", exclude_tiers=())}
        assert "style_list" in names
        assert "style_clone" in names
        for leftover in ("style_get_info", "style_update", "style_create", "style_import"):
            assert leftover not in names, f"page domain must not pull {leftover!r}"
        default_names = {t.name for t in registry.get_tools(doc=doc)}
        assert "style_list" not in default_names
        assert "style_clone" not in default_names

    def test_schemas(self):
        registry = get_tools()
        doc = WriterDocStub()
        schemas = registry.get_schemas("openai", doc=doc)
        assert schemas
        for s in schemas:
            assert "description" in s["function"]
            assert "parameters" in s["function"]


