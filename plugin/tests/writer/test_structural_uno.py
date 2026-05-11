from typing import Any

from plugin.testing_runner import setup, teardown, native_test
from plugin.tests.testing_utils import TestingFactory


_test_doc: Any = None
_test_ctx: Any = None


@setup
def setup_structural_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    _test_doc = TestingFactory.create_native_doc(ctx, "writer", hidden=True)
    assert _test_doc is not None, "Could not create hidden test writer document"


@teardown
def teardown_structural_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


@native_test
def test_structural_tools_execution():
    mock_ctx = TestingFactory.create_context(doc=_test_doc, ctx=_test_ctx, env="native", doc_type="writer")

    # Test ListBookmarks via registry
    from plugin.main import get_tools
    registry = get_tools()
    list_bm_tool = registry.get("list_bookmarks")
    assert list_bm_tool is not None, "list_bookmarks tool not found in registry"
    
    bm_res = list_bm_tool.execute(mock_ctx)
    assert bm_res["status"] == "ok", f"ListBookmarks failed: {bm_res}"
    assert isinstance(bm_res["bookmarks"], list), "ListBookmarks should return a list"

    list_sec_tool = registry.get("list_sections")
    assert list_sec_tool is not None, "list_sections (structural domain) should be registered"
    sec_res = list_sec_tool.execute(mock_ctx)
    assert sec_res["status"] == "ok", f"ListSections failed: {sec_res}"
    assert isinstance(sec_res["sections"], list), "ListSections should return a list"
