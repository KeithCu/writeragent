import unittest
import sys

# Mock uno for the pure python tests in this file specifically to allow import without native runner
try:
    import uno
except ImportError:
    class BaseStub:
        pass
    import types
    sys.modules['uno'] = types.ModuleType('uno')
    sys.modules['unohelper'] = types.ModuleType('unohelper')
    sys.modules['unohelper'].Base = BaseStub
    sys.modules['com'] = types.ModuleType('com')
    sys.modules['com.sun'] = types.ModuleType('sun')
    sys.modules['com.sun.star'] = types.ModuleType('star')
    sys.modules['com.sun.star.awt'] = types.ModuleType('awt')

    class MockListener(object):
        pass

    sys.modules['com.sun.star.awt'].XActionListener = MockListener
    sys.modules['com.sun.star.datatransfer'] = types.ModuleType('datatransfer')
    sys.modules['com.sun.star.datatransfer.clipboard'] = types.ModuleType('clipboard')
    class MockClipboardListener(object):
        pass
    sys.modules['com.sun.star.datatransfer.clipboard'].XClipboardListener = MockClipboardListener

from plugin.tests.testing_utils import ElementStub, WriterDocStub
from plugin.framework.document import (
    DocumentCache,
    build_heading_tree,
    resolve_locator,
    get_paragraph_ranges
)

class TestWriterNavigation(unittest.TestCase):
    def test_document_cache(self):
        model = WriterDocStub([])
        cache1 = DocumentCache.get(model)
        cache2 = DocumentCache.get(model)
        self.assertIs(cache1, cache2)
        
        DocumentCache.invalidate(model)
        cache3 = DocumentCache.get(model)
        self.assertIsNot(cache1, cache3)

    def test_build_heading_tree(self):
        elements = [
            ElementStub("H1", outline_level=1),
            ElementStub("P1"),
            ElementStub("H1.1", outline_level=2),
            ElementStub("P2"),
            ElementStub("H2", outline_level=1),
        ]
        doc = WriterDocStub(elements)
        tree = build_heading_tree(doc)
        
        # root -> [H1, H2]
        self.assertEqual(len(tree["children"]), 2)
        h1 = tree["children"][0]
        self.assertEqual(h1["text"], "H1")
        self.assertEqual(len(h1["children"]), 1)
        self.assertEqual(h1["children"][0]["text"], "H1.1")
        
        h2 = tree["children"][1]
        self.assertEqual(h2["text"], "H2")
        self.assertEqual(h2["body_paragraphs"], 0) # H2 is at end

    def test_get_paragraph_ranges_caching(self):
        doc = WriterDocStub([ElementStub("P1"), ElementStub("P2")])
        ranges1 = get_paragraph_ranges(doc)
        self.assertEqual(len(ranges1), 2)
        
        # Change underlying elements, but cache should remain
        doc.elements = [ElementStub("P3")]
        ranges2 = get_paragraph_ranges(doc)
        self.assertEqual(len(ranges2), 2)
        self.assertEqual(ranges1, ranges2)
        
        DocumentCache.invalidate(doc)
        ranges3 = get_paragraph_ranges(doc)
        self.assertEqual(len(ranges3), 1)

    def test_resolve_locator(self):
        doc = WriterDocStub([
            ElementStub("H1", outline_level=1),
            ElementStub("P1"),
            ElementStub("H2", outline_level=1),
            ElementStub("H2.1", outline_level=2),
        ])
        
        res = resolve_locator(doc, "paragraph:1")
        self.assertEqual(res["para_index"], 1)
        
        res = resolve_locator(doc, "heading:2")
        self.assertEqual(res["para_index"], 2) # H2 is at index 2
        
        res = resolve_locator(doc, "heading:2.1")
        self.assertEqual(res["para_index"], 3) # H2.1 is at index 3


try:
    from plugin.testing_runner import setup, teardown, native_test
    from plugin.framework.uno_context import get_desktop
    from plugin.modules.writer.navigation import NavigateHeading, GetSurroundings  # ToolBaseDummy: not exposed
except ImportError:
    setup, teardown, native_test = (lambda f: f), (lambda f: f), (lambda f: f)

_test_doc = None
_test_ctx = None

@setup
def setup_nav_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))

    text = _test_doc.getText()
    cursor = text.createTextCursor()

    # 0: Heading 1
    text.insertString(cursor, "Chapter 1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(cursor, 0, False)

    # 1: Paragraph
    text.insertString(cursor, "This is the first chapter.", False)
    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertControlCharacter(cursor, 0, False)

    # 2: Heading 2
    text.insertString(cursor, "Section 1.1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertControlCharacter(cursor, 0, False)

    # 3: Paragraph
    text.insertString(cursor, "This is a subsection.", False)
    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertControlCharacter(cursor, 0, False)

    # 4: Heading 1
    text.insertString(cursor, "Chapter 2", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(cursor, 0, False)

    # 5: Paragraph
    text.insertString(cursor, "This is the second chapter.", False)
    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertControlCharacter(cursor, 0, False)

@teardown
def teardown_nav_tests():
    if _test_doc:
        _test_doc.close(True)

class MockContext:
    def __init__(self, doc, ctx):
        self.doc = doc
        self.ctx = ctx
        self.services = MockServices(doc)

class MockServices:
    def __init__(self, doc):
        from plugin.framework.document import DocumentService
        from plugin.framework.event_bus import EventBus
        from plugin.modules.writer.proximity import ProximityService
        from plugin.modules.writer.bookmarks import BookmarkService
        from plugin.modules.writer.tree import TreeService
        from plugin.modules.writer.structural import ListSections

        self.events = EventBus()
        # DocumentService does not take constructor arguments; it uses the
        # active UNO context when needed.
        self.document = DocumentService()
        self.writer_bookmarks = BookmarkService(self.document, self.events)
        self.writer_tree = TreeService(self.document, self.writer_bookmarks, self.events)
        self.writer_structural = ListSections() # Simplified mock structural logic
        self.writer_proximity = ProximityService(self.document, self.writer_tree, self.writer_bookmarks)

@native_test
def test_navigate_heading():
    import pytest
    pytest.skip("navigate_heading tool currently not exposed to LLM/MCP API")

@native_test
def test_get_surroundings():
    import pytest
    pytest.skip("get_surroundings tool currently not exposed to LLM/MCP API")

if __name__ == "__main__":
    unittest.main()
