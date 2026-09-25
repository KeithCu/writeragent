import unittest

from plugin.tests.testing_utils import ElementStub, WriterDocStub

from plugin.doc.text_helpers import build_heading_tree
from plugin.doc.document_helpers import resolve_locator

class TestWriterNavigation(unittest.TestCase):
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


from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.navigation import NavHeading, NavSurroundings


def _populate_nav_doc(doc):
    text = doc.getText()
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


class MockContext:
    def __init__(self, doc, ctx):
        self.doc = doc
        self.ctx = ctx
        self.services = MockServices(doc)


class MockServices:
    def __init__(self, doc):
        from types import SimpleNamespace
        from plugin.doc.document_helpers import DocumentService
        from plugin.framework.event_bus import EventBus
        from plugin.writer.proximity import ProximityService
        from plugin.writer.specialized.bookmarks import BookmarkService
        from plugin.writer.tree import TreeService

        self.events = EventBus()
        self.document = DocumentService()
        s = SimpleNamespace()
        s.document = self.document
        s.events = self.events
        s.writer_bookmarks = BookmarkService()
        s.writer_tree = TreeService(s)
        s.writer_proximity = ProximityService(s)
        self.writer_bookmarks = s.writer_bookmarks
        self.writer_tree = s.writer_tree
        self.writer_proximity = s.writer_proximity


@native_test
@with_native_doc("writer")
def test_navigate_heading(ctx, doc):
    _populate_nav_doc(doc)
    mock_ctx = MockContext(doc, ctx)
    tool = NavHeading()
    res = tool.execute(mock_ctx, locator="paragraph:0", direction="next")
    assert res.get("status") == "ok", res
    assert res.get("heading", {}).get("text") == "Section 1.1"


@native_test
@with_native_doc("writer")
def test_get_surroundings(ctx, doc):
    _populate_nav_doc(doc)
    mock_ctx = MockContext(doc, ctx)
    tool = NavSurroundings()
    res = tool.execute(mock_ctx, locator="paragraph:2", radius=3)
    assert res.get("status") == "ok", res
    assert "paragraphs" in res


if __name__ == "__main__":
    unittest.main()
