
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("writer")
def test_structural_tools_execution(ctx, doc):
    mock_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")

    # Test ListBookmarks via registry
    from plugin.main import get_tools
    registry = get_tools()
    list_bm_tool = registry.get("bookmark_list")
    assert list_bm_tool is not None, "list_bookmarks tool not found in registry"
    
    bm_res = list_bm_tool.execute(mock_ctx)
    assert bm_res["status"] == "ok", f"ListBookmarks failed: {bm_res}"
    assert isinstance(bm_res["bookmarks"], list), "ListBookmarks should return a list"

    list_sec_tool = registry.get("section_list")
    assert list_sec_tool is not None, "list_sections (structural domain) should be registered"
    sec_res = list_sec_tool.execute(mock_ctx)
    assert sec_res["status"] == "ok", f"ListSections failed: {sec_res}"
    assert isinstance(sec_res["sections"], list), "ListSections should return a list"
