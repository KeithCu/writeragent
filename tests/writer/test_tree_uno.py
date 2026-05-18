from types import SimpleNamespace
from typing import Any

from plugin.testing_runner import setup, teardown, native_test
from plugin.tests.testing_utils import TestingFactory


_test_doc: Any = None
_test_ctx: Any = None


@setup
def setup_tree_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    _test_doc = TestingFactory.create_native_doc(ctx, "writer", hidden=True)
    assert _test_doc is not None, "Could not create hidden test writer document"

    # Setup doc content with headings
    text = _test_doc.getText()
    cursor = text.createTextCursor()

    # H1
    text.insertString(cursor, "H1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(cursor, 0, False)

    # P1
    text.insertString(cursor, "P1", False)
    text.insertControlCharacter(cursor, 0, False)

    # H1.1
    text.insertString(cursor, "H1.1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertControlCharacter(cursor, 0, False)


@teardown
def teardown_tree_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


@native_test
def test_tree_service_basic():
    from plugin.writer.tree import TreeService
    from plugin.writer.specialized.bookmarks import BookmarkService
    from plugin.framework.event_bus import EventBus
    from plugin.doc.document_helpers import DocumentService
    
    events = EventBus()
    doc_svc = DocumentService()
    services = SimpleNamespace()
    services.document = doc_svc
    services.events = events
    services.writer_bookmarks = BookmarkService(services)
    services.writer_tree = TreeService(services)
    tree_svc = services.writer_tree

    # 1. Test build_heading_tree from TreeService natively
    tree = tree_svc.build_heading_tree(_test_doc)
    assert tree is not None, "TreeService.build_heading_tree returned None"
    assert "children" in tree and len(tree["children"]) >= 1

    h1 = tree["children"][0]
    assert h1["text"] == "H1", "First child should be H1"

    # 2. Test resolve_writer_locator from TreeService natively
    res = tree_svc.resolve_writer_locator(_test_doc, "heading", "1.1")
    assert res is not None and res.get("para_index") == 2, f"Failed to resolve heading:1.1, got {res}"

    res = tree_svc.resolve_writer_locator(_test_doc, "heading_text", "H1.1")
    assert res is not None and res.get("para_index") == 2, f"Failed to resolve heading_text:H1.1, got {res}"
