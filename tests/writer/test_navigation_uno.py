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
    assert res.get("center_para_index") == 2, res
    assert res.get("range") == {"start": 0, "end": 5}, res
    texts = [p["text"] for p in res.get("paragraphs", [])]
    assert "Chapter 1" in texts, texts
    assert "Section 1.1" in texts, texts
    assert "This is the second chapter." in texts, texts


@native_test
@with_native_doc("writer")
def test_navigate_table(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()

    # 0: Paragraph before table
    text.insertString(cursor, "Before table.", False)
    text.insertControlCharacter(cursor, 0, False)

    # Insert Table
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(2, 2)
    table.setName("MyTable")
    text.insertTextContent(cursor, table, False)

    # 1: Table itself (anchor)

    # 2: Paragraph after table
    text.insertString(cursor, "After table.", False)
    text.insertControlCharacter(cursor, 0, False)

    mock_ctx = MockContext(doc, ctx)
    tool = NavSurroundings()

    # Use table:MyTable
    res = tool.execute(mock_ctx, locator="table:MyTable", radius=2)
    assert res.get("status") == "ok", res
    assert "paragraphs" in res

    # Test unknown table
    res2 = tool.execute(mock_ctx, locator="table:UnknownTable", radius=2)
    assert res2.get("status") == "error", res2
    assert "Table 'UnknownTable' not found" in res2.get("message", ""), res2
