# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live check: shape_group builds ShapeCollection via the service manager."""

from plugin.draw.bridge import DrawBridge
from plugin.draw.shapes import DeleteShape, GetDrawSummary, GroupShapes, UpsertShape
from plugin.draw.tree import find_shape_on_page
from plugin.framework.tool import ToolContext
from plugin.testing_runner import native_test
from tests.testing_utils import with_native_doc


def _group_two_rectangles(ctx, doc, doc_type):
    """Create two page shapes and group them. Returns the tool result and page."""
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    start = page.getCount()
    upsert = UpsertShape()
    for x in (1000, 5000):
        created = upsert.execute(
            tctx,
            action="create",
            shape_type="rectangle",
            page=0,
            x=x,
            y=1000,
            width=2000,
            height=1500,
            fill_color="red",
        )
        assert created.get("status") == "ok", created
    result = GroupShapes().execute(tctx, indices=[start, start + 1], page=0)
    assert result.get("status") == "ok", result
    assert "unknown service" not in str(result.get("message", ""))
    assert page.getCount() == start + 1, page.getCount()
    grouped = page.getByIndex(start)
    shape_type = grouped.getShapeType()
    assert "Group" in shape_type, shape_type
    assert result.get("name") == grouped.Name != ""
    return page, start, grouped


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_shape_group(ctx, doc):
    """Writer document factory cannot create ShapeCollection; grouping still works."""
    page, start, grouped = _group_two_rectangles(ctx, doc, "writer")
    # Confirms the result is a real group, not a leftover shape.
    page.ungroup(grouped)
    assert page.getCount() == start + 2


@native_test
@with_native_doc("draw")
def test_draw_shape_group(ctx, doc):
    page, start, grouped = _group_two_rectangles(ctx, doc, "draw")
    page.ungroup(grouped)
    assert page.getCount() == start + 2


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_shape_group(ctx, doc):
    """Calc document factory returns None for ShapeCollection."""
    page, start, grouped = _group_two_rectangles(ctx, doc, "calc")
    page.ungroup(grouped)
    assert page.getCount() == start + 2


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_shapes_target_active_sheet(ctx, doc):
    doc.getSheets().insertNewByName("Sheet2", 1)
    controller = doc.getCurrentController()
    sheet2 = doc.getSheets().getByIndex(1)
    controller.setActiveSheet(sheet2)

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="calc", services=None, caller="test")
    result = UpsertShape().execute(tctx, action="create", shape_type="rectangle", x=100, y=100, width=5000, height=5000)

    assert result["status"] == "ok"
    assert sheet2.getDrawPage().getCount() == 1
    assert doc.getSheets().getByIndex(0).getDrawPage().getCount() == 0
    assert result["page"] == 1


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_octagon_geometry_after_add(ctx, doc):
    """Writer reapplies CustomShapeGeometry after add so the shape paints.

    Draw and Impress must not do that second write (it can abort soffice).
    This test is the Writer exception: the process stays up and the geometry
    has a type plus Path or ViewBox.
    """
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    created = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="octagon",
        page=0,
        x=1000,
        y=1000,
        width=4000,
        height=4000,
    )
    assert created.get("status") == "ok", created
    page = DrawBridge(doc).get_pages().getByIndex(0)
    shape = page.getByIndex(page.getCount() - 1)
    geom = shape.getPropertyValue("CustomShapeGeometry")
    names = {str(prop.Name) for prop in geom}
    assert "Type" in names
    assert "Path" in names or "ViewBox" in names


@native_test
@with_native_doc("draw")
def test_shape_upsert_axis_aligned_line(ctx, doc):
    """Horizontal and vertical lines have a zero side. Rectangles do not."""
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    horizontal = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="line",
        page=0,
        x=500,
        y=500,
        width=4000,
        height=0,
    )
    assert horizontal.get("status") == "ok", horizontal
    shape = page.getByIndex(page.getCount() - 1)
    size = shape.getSize()
    assert size.Width == 4000
    assert size.Height == 0
    assert "LineShape" in shape.getShapeType()

    vertical = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="line",
        page=0,
        x=800,
        y=800,
        width=0,
        height=2500,
    )
    assert vertical.get("status") == "ok", vertical
    shape = page.getByIndex(page.getCount() - 1)
    size = shape.getSize()
    assert size.Width == 0
    assert size.Height == 2500

    rejected = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=100,
        y=100,
        width=4000,
        height=0,
    )
    assert rejected.get("status") == "error", rejected


@native_test
@with_native_doc("impress")
def test_impress_shape_off_slide_warning(ctx, doc):
    """Off-slide positions (x=30000 on a ~28000 slide) return ok with a warning."""
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="impress", services=None, caller="test")
    upsert = UpsertShape()

    # Off-slide position x=30000 on standard ~28000-wide slide
    res_off = upsert.execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=30000,
        y=1000,
        width=2000,
        height=1500,
    )
    assert res_off.get("status") == "ok", res_off
    assert "warnings" in res_off, res_off
    assert "Shape is outside the page bounds." in res_off["warnings"]

    # Normal position within slide boundaries has no warning
    res_in = upsert.execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=1000,
        y=1000,
        width=2000,
        height=1500,
    )
    assert res_in.get("status") == "ok", res_in
    assert "warnings" not in res_in, res_in


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_nested_group_shapes_summary_edit_delete(ctx, doc):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    upsert = UpsertShape()
    group_tool = GroupShapes()
    delete_tool = DeleteShape()
    summary_tool = GetDrawSummary()

    # Create four text shapes
    for y, text in ((1000, "text1"), (3000, "text2"), (5000, "text3"), (7000, "text4")):
        res = upsert.execute(
            tctx,
            action="create",
            shape_type="text",
            page=0,
            x=1000,
            y=y,
            width=2000,
            height=1000,
            text=text,
        )
        assert res.get("status") == "ok", res

    # Find rows by text to find the indices of the first two shapes
    summary = summary_tool.execute(tctx, page=0)
    assert summary.get("status") == "ok", summary
    text_to_idx = {s.get("text"): s["index"] for s in summary["shapes"]}
    assert "text1" in text_to_idx and "text2" in text_to_idx

    # Group the first two
    res_grp1 = group_tool.execute(tctx, indices=[text_to_idx["text1"], text_to_idx["text2"]], page=0)
    assert res_grp1.get("status") == "ok", res_grp1

    # Find the group and text3 to group them
    summary2 = summary_tool.execute(tctx, page=0)
    assert summary2.get("status") == "ok", summary2

    grp1_idx = None
    text3_idx = None
    for s in summary2["shapes"]:
        if isinstance(s["index"], int):
            if "GroupShape" in s["type"]:
                grp1_idx = s["index"]
            elif s.get("text") == "text3":
                text3_idx = s["index"]

    assert grp1_idx is not None and text3_idx is not None

    # Group that group with the third
    res_grp2 = group_tool.execute(tctx, indices=[grp1_idx, text3_idx], page=0)
    assert res_grp2.get("status") == "ok", res_grp2

    # Summary contains a two-dot path and a one-dot path
    summary3 = summary_tool.execute(tctx, page=0)
    assert summary3.get("status") == "ok", summary3

    indices = [s["index"] for s in summary3["shapes"]]
    one_dot_paths = [idx for idx in indices if isinstance(idx, str) and idx.count(".") == 1]
    two_dot_paths = [idx for idx in indices if isinstance(idx, str) and idx.count(".") == 2]
    assert len(one_dot_paths) >= 1
    assert len(two_dot_paths) >= 1

    # Pick a two-dot path (the trailing child so deleting it removes that address from the summary)
    target_path = two_dot_paths[-1]

    # shape_upsert edit of the two-dot path with text="stripe" returns that path,
    # and the UNO child's getString() is "stripe"
    edit_res = upsert.execute(tctx, action="edit", index=target_path, text="stripe", page=0)
    assert edit_res.get("status") == "ok", edit_res
    assert edit_res.get("index") == target_path

    _addr, child_shape, parent_group, find_err = find_shape_on_page(page, index=target_path)
    assert find_err is None
    assert child_shape is not None
    assert parent_group is not None
    assert child_shape.getString() == "stripe"

    # shape_delete of that path leaves page.getCount() unchanged, drops the inner
    # group's count by 1, and the next summary no longer contains that address.
    page_count_before = page.getCount()
    inner_count_before = parent_group.getCount()

    del_res = delete_tool.execute(tctx, index=target_path, page=0)
    assert del_res.get("status") == "ok", del_res
    assert page.getCount() == page_count_before
    assert parent_group.getCount() == inner_count_before - 1

    summary4 = summary_tool.execute(tctx, page=0)
    summary4_indices = [s["index"] for s in summary4["shapes"]]
    assert target_path not in summary4_indices


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_align_two_rectangles(ctx, doc):
    from plugin.draw.shapes import AlignShapes, UpsertShape
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    page = doc.getDrawPages().getByIndex(0)
    start = page.getCount()
    upsert = UpsertShape()
    for x in (1000, 6000):
        out = upsert.execute(
            tctx,
            action="create",
            shape_type="rectangle",
            page=0,
            x=x,
            y=1000,
            width=2000,
            height=1500,
        )
        assert out.get("status") == "ok", out
    aligned = AlignShapes().execute(tctx, indices=[start, start + 1], alignment="left", page=0)
    assert aligned.get("status") == "ok", aligned


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_shape_diagram_one_node(ctx, doc):
    from plugin.draw.shapes import CreateDiagram
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="calc", services=None, caller="test")
    out = CreateDiagram().execute(
        tctx,
        page=0,
        layout="horizontal_flow",
        nodes=[{"id": "a", "text": "Only"}],
    )
    assert out.get("status") == "ok", out


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_shape_upsert_names_new_shapes(ctx, doc):
    from plugin.draw.bridge import DrawBridge
    from plugin.draw.shapes import UpsertShape
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    upsert = UpsertShape()
    first = upsert.execute(tctx, action="create", shape_type="rectangle", page=0, x=1000, y=1000, width=2000, height=1500)
    second = upsert.execute(tctx, action="create", shape_type="rectangle", page=0, x=4000, y=1000, width=2000, height=1500)
    assert first.get("status") == "ok", first
    assert second.get("status") == "ok", second

    page = DrawBridge(doc).get_pages().getByIndex(0)
    assert first["name"] and second["name"]
    assert first["name"] != second["name"]
    assert first["name"] == page.getByIndex(first["index"]).Name
    assert second["name"] == page.getByIndex(second["index"]).Name

    edited = upsert.execute(tctx, action="edit", page=0, name=first["name"], text="x")
    assert edited.get("status") == "ok", edited
    assert page.getByIndex(first["index"]).getString() == "x"

    count_before = page.getCount()
    dup = upsert.execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=7000,
        y=1000,
        width=2000,
        height=1500,
        name=first["name"],
    )
    assert dup.get("status") == "error"
    assert dup.get("code") == "INVALID_PARAM"
    assert page.getCount() == count_before


@native_test
@with_native_doc("draw")
def test_shape_results_summarize_to_page_counts_uno(ctx, doc):
    from plugin.doc.specialized_catalog import InnerToolCall
    from plugin.doc.specialized_shapes_context import summarize_shape_calls
    from plugin.draw.shapes import ConnectShapes, CreateDiagram, UpsertShape
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = doc.getDrawPages().getByIndex(0)
    start_count = page.getCount()

    upsert = UpsertShape()
    r1 = upsert.execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=1000,
        y=1000,
        width=2000,
        height=1200,
        name="Start",
    )
    assert r1.get("status") == "ok", r1
    r2 = upsert.execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=5000,
        y=1000,
        width=2000,
        height=1200,
    )
    assert r2.get("status") == "ok", r2
    r3 = ConnectShapes().execute(tctx, start=start_count + 1, end="Start", page=0)
    assert r3.get("status") == "ok", r3
    r4 = CreateDiagram().execute(
        tctx,
        page=0,
        layout="horizontal_flow",
        nodes=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
        connections=[{"from": "a", "to": "b"}],
    )
    assert r4.get("status") == "ok", r4

    assert UpsertShape.name and ConnectShapes.name and CreateDiagram.name
    summary = summarize_shape_calls([
        InnerToolCall(UpsertShape.name, r1, True),
        InnerToolCall(UpsertShape.name, r2, True),
        InnerToolCall(ConnectShapes.name, r3, True),
        InnerToolCall(CreateDiagram.name, r4, True),
    ])
    assert summary["shapes_created"] == 4
    assert summary["connectors_created"] == 2
    assert summary["shapes_created"] + summary["connectors_created"] == page.getCount() - start_count
    assert summary["created_names"] == ["Start", r2["name"]]
    assert summary["pages"] == [0]
    assert page.getByIndex(start_count + 1).Name == r2["name"] != ""


@native_test
@with_native_doc("draw")
def test_shape_upsert_fill_and_line_style_enums(ctx, doc):
    """FillStyle, LineStyle, and TextFitToSizeType enums apply and read back cleanly."""
    import uno

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    upsert = UpsertShape()

    rect_res = upsert.execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=1000,
        y=1000,
        width=3000,
        height=2000,
        fill_style="solid",
        fill_color="#FF0000",
        line_color="#0000FF",
        line_width=100,
        text="hi",
    )
    assert rect_res.get("status") == "ok", rect_res
    assert "warnings" not in rect_res, rect_res
    rect = page.getByIndex(rect_res["index"])
    assert rect.getPropertyValue("FillStyle") == uno.Enum("com.sun.star.drawing.FillStyle", "SOLID")
    assert rect.getPropertyValue("LineStyle") == uno.Enum("com.sun.star.drawing.LineStyle", "SOLID")
    assert rect.getPropertyValue("TextFitToSize") == uno.Enum("com.sun.star.drawing.TextFitToSizeType", "AUTOFIT")

    edit_res = upsert.execute(tctx, action="edit", page=0, index=rect_res["index"], fill_style="none")
    assert edit_res.get("status") == "ok", edit_res
    assert "warnings" not in edit_res, edit_res
    assert rect.getPropertyValue("FillStyle") == uno.Enum("com.sun.star.drawing.FillStyle", "NONE")

    line_res = upsert.execute(
        tctx,
        action="create",
        shape_type="line",
        page=0,
        x=1000,
        y=4000,
        width=3000,
        height=0,
        line_color="#00FF00",
        line_width=50,
    )
    assert line_res.get("status") == "ok", line_res
    assert "warnings" not in line_res, line_res
    line = page.getByIndex(line_res["index"])
    assert line.getPropertyValue("LineStyle") == uno.Enum("com.sun.star.drawing.LineStyle", "SOLID")


@native_test
@with_native_doc("draw")
def test_shape_upsert_negative_size_and_line_direction(ctx, doc):
    """Negative width/height normalize span and preserve LineShape start->end direction."""
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    upsert = UpsertShape()

    rtl_res = upsert.execute(
        tctx,
        action="create",
        shape_type="line",
        page=0,
        x=5000,
        y=3000,
        width=-2890,
        height=0,
    )
    assert rtl_res.get("status") == "ok", rtl_res
    rtl_line = page.getByIndex(rtl_res["index"])
    assert (rtl_line.getPosition().X, rtl_line.getPosition().Y) == (2110, 3000)
    assert (rtl_line.getSize().Width, rtl_line.getSize().Height) == (2890, 0)
    rtl_poly = [[(p.X, p.Y) for p in seq] for seq in rtl_line.getPropertyValue("PolyPolygon")]
    assert rtl_poly == [[(5000, 3000), (2110, 3000)]]

    diag_res = upsert.execute(
        tctx,
        action="create",
        shape_type="line",
        page=0,
        x=5000,
        y=3000,
        width=-2000,
        height=-1000,
    )
    assert diag_res.get("status") == "ok", diag_res
    diag_line = page.getByIndex(diag_res["index"])
    assert (diag_line.getPosition().X, diag_line.getPosition().Y) == (3000, 2000)
    assert (diag_line.getSize().Width, diag_line.getSize().Height) == (2000, 1000)
    diag_poly = [[(p.X, p.Y) for p in seq] for seq in diag_line.getPropertyValue("PolyPolygon")]
    assert diag_poly == [[(5000, 3000), (3000, 2000)]]

    rect_res = upsert.execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=5000,
        y=3000,
        width=-2000,
        height=-1000,
    )
    assert rect_res.get("status") == "ok", rect_res
    rect = page.getByIndex(rect_res["index"])
    assert (rect.getPosition().X, rect.getPosition().Y) == (3000, 2000)
    assert (rect.getSize().Width, rect.getSize().Height) == (2000, 1000)

    rect_edit = upsert.execute(tctx, action="edit", page=0, index=rect_res["index"], width=-1500)
    assert rect_edit.get("status") == "ok", rect_edit
    assert (rect.getPosition().X, rect.getPosition().Y) == (1500, 2000)
    assert (rect.getSize().Width, rect.getSize().Height) == (1500, 1000)

    line_edit = upsert.execute(
        tctx,
        action="edit",
        page=0,
        index=rtl_res["index"],
        x=6000,
        y=4000,
        width=-1000,
        height=500,
    )
    assert line_edit.get("status") == "ok", line_edit
    assert (rtl_line.getPosition().X, rtl_line.getPosition().Y) == (5000, 4000)
    assert (rtl_line.getSize().Width, rtl_line.getSize().Height) == (1000, 500)
    edited_poly = [[(p.X, p.Y) for p in seq] for seq in rtl_line.getPropertyValue("PolyPolygon")]
    assert edited_poly == [[(6000, 4000), (5000, 4500)]]

