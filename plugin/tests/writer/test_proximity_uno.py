from types import SimpleNamespace
from typing import Any

from plugin.doc.document_helpers import (
    get_paragraph_ranges,
    get_document_length,
    resolve_locator,
)
from plugin.testing_runner import setup, teardown, native_test
from plugin.tests.testing_utils import TestingFactory


_test_doc: Any = None
_test_ctx: Any = None


@setup
def setup_proximity_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    _test_doc = TestingFactory.create_native_doc(ctx, "writer", hidden=True)
    assert _test_doc is not None, "Could not create hidden test writer document"

    # Setup minimal doc content
    text = _test_doc.getText()
    cursor = text.createTextCursor()
    text.insertString(cursor, "P1", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "P2", False)


@teardown
def teardown_proximity_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


@native_test
def test_proximity_service():
    from plugin.writer.proximity import ProximityService
    from plugin.writer.specialized.bookmarks import BookmarkService
    from plugin.writer.tree import TreeService
    from plugin.framework.event_bus import EventBus
    from plugin.writer.ops import find_paragraph_for_range as ops_find_paragraph_for_range

    events = EventBus()

    class DocSvcAdapter:
        def doc_key(self, doc):
            return id(doc)
        def get_document_length(self, model):
            return get_document_length(model)
        def resolve_locator(self, doc, locator):
            return resolve_locator(doc, locator)
        def get_paragraph_ranges(self, doc):
            return get_paragraph_ranges(doc)
        def find_paragraph_for_range(self, anchor, para_ranges, text_obj):
            return ops_find_paragraph_for_range(anchor, para_ranges, text_obj)
        def yield_to_gui(self):
            pass

    services = SimpleNamespace()
    services.document = DocSvcAdapter()
    services.events = events
    services.writer_bookmarks = BookmarkService(services)
    services.writer_tree = TreeService(services)
    services.writer_proximity = ProximityService(services)

    res = services.writer_proximity.get_surroundings(_test_doc, "paragraph:0", radius=0)
    assert res is not None and res.get("center_para_index") == 0, f"ProximityService get_surroundings failed: {res}"
