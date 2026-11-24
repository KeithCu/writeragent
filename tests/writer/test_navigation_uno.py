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


@native_test
@with_native_doc("writer", reuse=False)
def test_get_surroundings_frames_include_text_boxes_uno(ctx, doc):
    from com.sun.star.awt import Point, Size
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_PAGE, AT_PARAGRAPH

    text = doc.getText()
    cur = text.createTextCursor()
    for idx in range(4):
        text.insertString(cur, f"Paragraph {idx}", False)
        if idx < 3:
            text.insertControlCharacter(cur, 0, False)

    para_enum = text.createEnumeration()
    paras = []
    while para_enum.hasMoreElements():
        paras.append(para_enum.nextElement())

    plain = doc.createInstance("com.sun.star.text.TextFrame")
    plain.setPropertyValue("Size", Size(3000, 1500))
    plain.setPropertyValue("AnchorType", AT_PARAGRAPH)
    text.insertTextContent(paras[1].getStart(), plain, False)
    plain.setName("PlainFrame")

    def _add_box(shape_name: str, anchor_type, anchor_range, *, page_no: int | None = None) -> str:
        shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
        shape.setName(shape_name)
        shape.setSize(Size(3000, 1500))
        shape.setPosition(Point(500, 500))
        shape.setPropertyValue("AnchorType", anchor_type)
        if page_no is not None:
            shape.setPropertyValue("AnchorPageNo", page_no)
        text.insertTextContent(anchor_range, shape, False)
        shape.setPropertyValue("TextBox", True)
        shape.setString(shape_name)
        return shape.getPropertyValue("TextBoxContent").getName()

    para_box_name = _add_box("ParaBox", AT_PARAGRAPH, paras[0].getStart())
    as_char_box_name = _add_box("AsCharBox", AS_CHARACTER, paras[2].getEnd())
    page_box_name = _add_box("PageBox", AT_PAGE, paras[0].getStart(), page_no=1)

    res = NavSurroundings().execute(MockContext(doc, ctx), locator="paragraph:1", radius=5, include=["frames"])
    assert res.get("status") == "ok", res
    by_name = {f["name"]: f for f in res["frames"]}
    assert "PlainFrame" in by_name, by_name
    assert by_name["PlainFrame"]["paragraph_index"] == 1
    assert para_box_name in by_name, by_name
    assert by_name[para_box_name]["paragraph_index"] == 0
    assert as_char_box_name in by_name, by_name
    assert by_name[as_char_box_name]["paragraph_index"] == 2
    assert page_box_name not in by_name, by_name


@native_test
@with_native_doc("writer")
def test_surroundings_hides_tracked_deletions_uno(ctx, doc):
    from plugin.tests.testing_utils import strike_across_paragraphs

    text = doc.getText()
    strike_across_paragraphs(doc, text)
    enum = text.createEnumeration()
    paras = []
    while enum.hasMoreElements():
        paras.append(enum.nextElement())
    p2_cur = text.createTextCursorByRange(paras[2])
    p2_cur.setPropertyValue("ParaStyleName", "Heading 1")

    tool = NavSurroundings()
    mock_ctx = MockContext(doc, ctx)
    res0 = tool.execute(mock_ctx, locator="paragraph:2", radius=0, include=["paragraphs"])
    assert res0.get("status") == "ok", res0
    assert [(p["text"], p["outline_level"]) for p in res0["paragraphs"]] == [("", 0)]

    res1 = tool.execute(mock_ctx, locator="paragraph:2", radius=1, include=["paragraphs"])
    assert res1.get("status") == "ok", res1
    texts = [p["text"] for p in res1["paragraphs"]]
    assert all("gone" not in t for t in texts), texts
    assert texts == ["Start ", "", "Tail"]
