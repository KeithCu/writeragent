
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("writer")
def test_get_page_for_paragraph_with_table(ctx, doc):
    """Test get_page_for_paragraph resolves correctly when tables exist."""
    import time
    import uno
    from plugin.doc.document_helpers import DocumentService
    text = doc.getText()
    cursor = text.createTextCursor()

    # 0: Page 1, paragraph
    text.insertString(cursor, "First paragraph\n", False)

    # 1: Page 1, table (2x2)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(2, 2)
    text.insertTextContent(cursor, table, False)

    # 2: Page 1, paragraph after table
    text.insertString(cursor, "Paragraph after table", False)
    text.insertControlCharacter(cursor, 0, False)

    # 3: Page 2, paragraph
    cursor.setPropertyValue("BreakType", uno.Enum("com.sun.star.style.BreakType", "PAGE_BEFORE"))
    text.insertString(cursor, "Page 2 paragraph", False)

    # Let layout settle
    # Give it a moment to layout pages, as getPage relies on view layout
    time.sleep(0.5)

    ds = DocumentService()

    # Test paragraph before table
    page = ds.get_page_for_paragraph(doc, 0)
    assert page == 1

    # Test table itself (index 1)
    page = ds.get_page_for_paragraph(doc, 1)
    assert page == 1

    # Test paragraph after table (index 2)
    page = ds.get_page_for_paragraph(doc, 2)
    assert page == 1

    # Test paragraph on page 2 (index 3)
    page = ds.get_page_for_paragraph(doc, 3)
    assert page == 2


@native_test
@with_native_doc("writer")
def test_writer_snapshot_lists_chart_frame_image_and_names_delete_them_uno(ctx, doc):
    """Writer snapshot lists chart/frame/image with names that chart_delete and shape_delete accept."""
    from plugin.calc.charts import ChartDelete, ChartUpsert
    from plugin.doc.document_helpers import get_document_context_for_chat
    from plugin.draw.shapes import DeleteShape
    from plugin.tests.testing_utils import TestingFactory

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    text.insertString(cur, "Intro paragraph here.", False)

    chart_res = ChartUpsert().execute(
        tool_ctx,
        chart_type="bar",
        headers=["k", "v"],
        rows=[["a", 1], ["b", 2]],
    )
    assert chart_res.get("status") == "ok", chart_res
    chart_name = chart_res["name"]

    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.Name = "Frame1"
    cur.gotoEnd(False)
    text.insertTextContent(cur, frame, False)

    graphic = doc.createInstance("com.sun.star.text.TextGraphicObject")
    graphic.Name = "Image1"
    cur.gotoEnd(False)
    text.insertTextContent(cur, graphic, False)

    snap = get_document_context_for_chat(doc, ctx=ctx)
    lines = snap.splitlines()
    assert any(chart_name in line and "Chart" in line for line in lines), snap
    assert any("Frame1" in line and "TextFrame" in line for line in lines), snap
    assert any("Image1" in line and "Image" in line for line in lines), snap

    del_chart = ChartDelete().execute(tool_ctx, name=chart_name)
    assert del_chart.get("status") == "ok", del_chart
    assert not doc.getEmbeddedObjects().hasByName(chart_name)

    del_frame = DeleteShape().execute(tool_ctx, name="Frame1")
    assert del_frame.get("status") == "ok", del_frame
    assert not doc.getTextFrames().hasByName("Frame1")

    fresh_snap = get_document_context_for_chat(doc, ctx=ctx)
    assert chart_name not in fresh_snap
    assert "Frame1" not in fresh_snap

