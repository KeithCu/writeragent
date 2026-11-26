# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live check: shape_group builds ShapeCollection via the service manager."""

import gc
import os
import re
import shutil
import tempfile

import uno
from com.sun.star.beans import PropertyValue

from plugin.doc.compound_undo import undo_titles
from plugin.draw.bridge import DrawBridge
from plugin.draw.shapes import AlignShapes, DeleteShape, DistributeShapes, GetDrawSummary, GroupShapes, UpsertShape
from plugin.draw.tree import GetDrawTree, find_shape_on_page
from plugin.framework.tool import ToolContext
from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from tests.testing_utils import logo_32_path, with_native_doc


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
    """Writer reapplies CustomShapeGeometry after add so the geometry has Type plus Path or ViewBox."""
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
@with_native_doc("draw", reuse=False)
def test_draw_octagon_geometry_after_add(ctx, doc):
    """Draw reapplies CustomShapeGeometry after add so ViewBox and Path expand and shapes paint."""
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    for idx, shape_type in enumerate(("octagon", "round-rectangle", "trapezoid")):
        created = UpsertShape().execute(
            tctx,
            action="create",
            shape_type=shape_type,
            page=0,
            x=1000 + idx * 5000,
            y=1000,
            width=4000,
            height=4000,
            fill_color="#FF0000",
        )
        assert created.get("status") == "ok", (shape_type, created)
        shape = page.getByIndex(page.getCount() - 1)
        geom = {str(prop.Name): prop.Value for prop in shape.getPropertyValue("CustomShapeGeometry")}
        assert geom.get("Type") == shape_type, (shape_type, geom)
        assert geom.get("ViewBox") is not None, (shape_type, geom)
        assert geom.get("Path"), (shape_type, geom)
        assert getattr(shape.getPropertyValue("FillStyle"), "value", None) == "SOLID"

    tree = GetDrawTree().execute(tctx, page=0)["tree"]
    assert len(tree) == 3
    for node in tree:
        assert node["style"]["FillStyle"] == "SOLID"
        assert "LineStyle" in node["style"]
        assert "custom_geometry_empty" not in node


@native_test
@with_native_doc("impress", reuse=False)
def test_impress_octagon_geometry_after_add(ctx, doc):
    """Impress reapplies CustomShapeGeometry after add so ViewBox and Path expand and shapes paint."""
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="impress", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    created = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="octagon",
        page=0,
        x=1000,
        y=1000,
        width=4000,
        height=4000,
        fill_color="#FF0000",
    )
    assert created.get("status") == "ok", created
    shape = page.getByIndex(page.getCount() - 1)
    geom = {str(prop.Name): prop.Value for prop in shape.getPropertyValue("CustomShapeGeometry")}
    assert geom.get("Type") == "octagon", geom
    assert geom.get("ViewBox") is not None, geom
    assert geom.get("Path"), geom
    assert getattr(shape.getPropertyValue("FillStyle"), "value", None) == "SOLID"


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


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_shape_upsert_negative_line_direction(ctx, doc):
    """Writer lines with negative width preserve direction and stay in anchor coords."""
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
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
    line = page.getByIndex(rtl_res["index"])
    pos1 = line.getPosition()
    size1 = line.getSize()
    assert abs(pos1.X - 2110) <= 2
    assert abs(pos1.Y - 3000) <= 2
    assert abs(size1.Width - 2890) <= 2
    assert size1.Height == 0
    poly1 = [[(p.X, p.Y) for p in seq] for seq in line.getPropertyValue("PolyPolygon")]
    assert len(poly1) == 1 and len(poly1[0]) == 2
    assert poly1[0][0][0] > poly1[0][1][0]
    assert abs((poly1[0][0][0] - poly1[0][1][0]) - 2890) <= 2

    # Edit with negative width only (no x/y): stays at Y without a margin jump and keeps RTL.
    edit_neg = upsert.execute(tctx, action="edit", page=0, index=rtl_res["index"], width=-1000)
    assert edit_neg.get("status") == "ok", edit_neg
    pos2 = line.getPosition()
    assert abs(pos2.Y - pos1.Y) <= 2
    assert abs(pos2.X - (pos1.X - 1000)) <= 2
    poly2 = [[(p.X, p.Y) for p in seq] for seq in line.getPropertyValue("PolyPolygon")]
    assert poly2[0][0][0] > poly2[0][1][0]
    assert abs((poly2[0][0][0] - poly2[0][1][0]) - 1000) <= 2

    # Edit with positive width only (no x/y): getPosition() stays unchanged and direction flips LTR.
    edit_pos = upsert.execute(tctx, action="edit", page=0, index=rtl_res["index"], width=1500)
    assert edit_pos.get("status") == "ok", edit_pos
    pos3 = line.getPosition()
    assert abs(pos3.X - pos2.X) <= 2
    assert abs(pos3.Y - pos2.Y) <= 2
    poly3 = [[(p.X, p.Y) for p in seq] for seq in line.getPropertyValue("PolyPolygon")]
    assert poly3[0][0][0] < poly3[0][1][0]
    assert abs((poly3[0][1][0] - poly3[0][0][0]) - 1500) <= 2


def _export_shape_svg(ctx, shape) -> str:
    """Export a single shape to SVG and return the XML string."""
    tmpdir = tempfile.mkdtemp(prefix="shape_svg_")
    try:
        out_path = os.path.join(tmpdir, "shape.svg")
        exporter = ctx.getServiceManager().createInstanceWithContext("com.sun.star.drawing.GraphicExportFilter", ctx)
        exporter.setSourceDocument(shape)
        exporter.filter(
            (
                PropertyValue(Name="URL", Value=uno.systemPathToFileUrl(out_path)),
                PropertyValue(Name="MediaType", Value="image/svg+xml"),
            )
        )
        with open(out_path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _svg_text_positions(ctx, shape) -> list[tuple[int, int]]:
    """Export a single shape to SVG and return (x, y) of each TextPosition element."""
    svg = _export_shape_svg(ctx, shape)
    positions: list[tuple[int, int]] = []
    for match in re.finditer(r'<tspan[^>]*class="TextPosition"[^>]*>', svg):
        tag = match.group(0)
        x_m = re.search(r'\bx="(-?\d+)"', tag)
        y_m = re.search(r'\by="(-?\d+)"', tag)
        if x_m and y_m:
            positions.append((int(x_m.group(1)), int(y_m.group(1))))
    return positions


def _svg_corner_distance(ctx, shape) -> float:
    """Minimum Euclidean distance from the fill path's top-left bounding corner to any path point."""
    import math

    svg = _export_shape_svg(ctx, shape)
    pts: list[tuple[float, float]] = []
    for path_match in re.finditer(r'<path[^>]*\bfill="(?!none)[^"]*"[^>]*\bd="([^"]+)"', svg):
        for x_s, y_s in re.findall(r"(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)", path_match.group(1)):
            pts.append((float(x_s), float(y_s)))
    assert pts, f"No filled path points found in SVG: {svg[:500]}"
    min_x = min(x for x, _y in pts)
    min_y = min(y for _x, y in pts)
    return min(math.hypot(x - min_x, y - min_y) for x, y in pts)


def _iter_paragraphs_and_portions(shape):
    """Yield (paragraph, portion) pairs for all text in a shape."""
    para_enum = shape.getText().createEnumeration()
    while enum_has_more(para_enum):
        para = para_enum.nextElement()
        port_enum = para.createEnumeration()
        while enum_has_more(port_enum):
            yield para, port_enum.nextElement()


@native_test
@with_native_doc("draw")
def test_shape_upsert_text_style_and_alignment(ctx, doc):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    upsert = UpsertShape()

    res = upsert.execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=1000,
        y=1000,
        width=8000,
        height=4000,
        text="Hi",
        font_size=18,
        bold=True,
        italic=True,
        h_align="left",
        v_align="top",
        corner_radius=500,
    )
    assert res.get("status") == "ok", res
    assert not res.get("warnings"), res

    shape = page.getByIndex(res["index"])
    italic_enum = uno.Enum("com.sun.star.awt.FontSlant", "ITALIC")
    items = list(_iter_paragraphs_and_portions(shape))
    assert items
    for para, port in items:
        assert port.getPropertyValue("CharWeight") == 150.0
        assert port.getPropertyValue("CharPosture") == italic_enum
        assert para.getPropertyValue("ParaAdjust") == 0
    assert shape.getPropertyValue("TextVerticalAdjust") == uno.Enum("com.sun.star.drawing.TextVerticalAdjust", "TOP")
    corner_dist = _svg_corner_distance(ctx, shape)
    assert 150 <= corner_dist <= 300, corner_dist

    positions = _svg_text_positions(ctx, shape)
    assert positions
    x0, y0 = positions[0]
    assert x0 < 8000 // 4, positions
    assert y0 < 4000 // 2, positions

    edit_res = upsert.execute(
        tctx,
        action="edit",
        page=0,
        index=res["index"],
        bold=False,
        h_align="right",
        v_align="bottom",
        corner_radius=0,
    )
    assert edit_res.get("status") == "ok", edit_res
    assert not edit_res.get("warnings"), edit_res
    for _para, port in _iter_paragraphs_and_portions(shape):
        assert port.getPropertyValue("CharWeight") == 100.0
    assert _svg_corner_distance(ctx, shape) < 20

    positions2 = _svg_text_positions(ctx, shape)
    assert positions2
    x1, y1 = positions2[0]
    assert x1 > 8000 // 2, positions2
    assert y1 > 4000 // 2, positions2


@native_test
@with_native_doc("draw")
def test_shape_upsert_corner_radius_on_ellipse_warns(ctx, doc):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    res = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="ellipse",
        page=0,
        x=1000,
        y=1000,
        width=4000,
        height=2000,
        corner_radius=300,
    )
    assert res.get("status") == "ok", res
    warnings = res.get("warnings") or []
    assert any("corner_radius" in str(w) for w in warnings), res


@native_test
@with_native_doc("draw")
def test_shape_upsert_round_rectangle_corner_radius(ctx, doc):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    upsert = UpsertShape()

    default_res = upsert.execute(
        tctx,
        action="create",
        shape_type="round-rectangle",
        page=0,
        x=1000,
        y=1000,
        width=4000,
        height=2000,
    )
    assert default_res.get("status") == "ok", default_res
    assert not default_res.get("warnings"), default_res

    custom_res = upsert.execute(
        tctx,
        action="create",
        shape_type="round-rectangle",
        page=0,
        x=1000,
        y=4000,
        width=4000,
        height=2000,
        corner_radius=300,
    )
    assert custom_res.get("status") == "ok", custom_res
    assert not custom_res.get("warnings"), custom_res

    default_dist = _svg_corner_distance(ctx, page.getByIndex(default_res["index"]))
    custom_dist = _svg_corner_distance(ctx, page.getByIndex(custom_res["index"]))
    assert 0 < custom_dist < default_dist, (custom_dist, default_dist)


def _check_custom_shape_text_defaults(ctx, doc, doc_type: str) -> None:
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    upsert = UpsertShape()

    # 1. Short label: centred horizontally and vertically with no warnings.
    res1 = upsert.execute(
        tctx,
        action="create",
        shape_type="flowchart-process",
        page=0,
        x=1000,
        y=1000,
        width=8000,
        height=4000,
        text="Hi",
        font_size=18,
    )
    assert res1.get("status") == "ok", res1
    assert not res1.get("warnings"), res1
    shape1 = page.getByIndex(res1["index"])
    pos1 = _svg_text_positions(ctx, shape1)
    assert len(pos1) == 1, pos1
    assert 3000 < pos1[0][0] < 4500, pos1
    assert 1500 < pos1[0][1] < 3000, pos1

    # 2. Long label with defaults: wraps across multiple lines.
    long_text = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    res2 = upsert.execute(
        tctx,
        action="create",
        shape_type="flowchart-process",
        page=0,
        x=1000,
        y=6000,
        width=8000,
        height=4000,
        text=long_text,
        font_size=18,
    )
    assert res2.get("status") == "ok", res2
    shape2 = page.getByIndex(res2["index"])
    pos2 = _svg_text_positions(ctx, shape2)
    assert len({y for _x, y in pos2}) >= 2, pos2

    # 3. Explicit overrides: top-left and unwrapped.
    res3 = upsert.execute(
        tctx,
        action="create",
        shape_type="flowchart-process",
        page=0,
        x=1000,
        y=11000,
        width=8000,
        height=4000,
        text=long_text,
        font_size=18,
        h_align="left",
        v_align="top",
        wrap_text=False,
    )
    assert res3.get("status") == "ok", res3
    shape3 = page.getByIndex(res3["index"])
    pos3 = _svg_text_positions(ctx, shape3)
    assert len({y for _x, y in pos3}) == 1, pos3
    assert pos3[0][0] < 1000, pos3
    assert pos3[0][1] < 1500, pos3

    # 4. Create empty (no text, no font_size), then edit text: keeps size and centring.
    res4 = upsert.execute(
        tctx,
        action="create",
        shape_type="flowchart-process",
        page=0,
        x=1000,
        y=16000,
        width=8000,
        height=4000,
    )
    assert res4.get("status") == "ok", res4
    shape4 = page.getByIndex(res4["index"])
    sz4 = shape4.getSize()
    assert abs(sz4.Width - 8000) <= 2 and abs(sz4.Height - 4000) <= 2, (sz4.Width, sz4.Height)
    del shape4
    gc.collect()

    res4_edit = upsert.execute(
        tctx,
        action="edit",
        index=res4["index"],
        page=0,
        text="Hi",
        font_size=18,
    )
    assert res4_edit.get("status") == "ok", res4_edit
    shape4 = page.getByIndex(res4["index"])
    pos4 = _svg_text_positions(ctx, shape4)
    assert len(pos4) == 1, pos4
    assert 3000 < pos4[0][0] < 4500, pos4
    assert 1500 < pos4[0][1] < 3000, pos4

    # 5. rectangle and ellipse wrap long labels inside the shape at a fixed font_size.
    for idx, shape_type in enumerate(("rectangle", "ellipse")):
        res5 = upsert.execute(
            tctx,
            action="create",
            shape_type=shape_type,
            page=0,
            x=10000,
            y=1000 + idx * 5000,
            width=8000,
            height=4000,
            text=long_text,
            font_size=18,
        )
        assert res5.get("status") == "ok", res5
        pos5 = _svg_text_positions(ctx, page.getByIndex(res5["index"]))
        assert len({y for _x, y in pos5}) >= 2, (shape_type, pos5)
        assert all(x >= 0 for x, _y in pos5), (shape_type, pos5)

    # 6. Empty custom shape keeps its size even after a raw setString("Hi").
    res6 = upsert.execute(
        tctx,
        action="create",
        shape_type="flowchart-process",
        page=0,
        x=10000,
        y=11000,
        width=8000,
        height=4000,
    )
    assert res6.get("status") == "ok", res6
    shape6 = page.getByIndex(res6["index"])
    shape6.setString("Hi")
    sz6 = shape6.getSize()
    assert abs(sz6.Width - 8000) <= 2 and abs(sz6.Height - 4000) <= 2, (sz6.Width, sz6.Height)


@native_test
@with_native_doc("draw")
def test_fill_draw_fields_keeps_custom_shape_center(ctx, doc):
    from plugin.draw.field_fill import FillDrawFields

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    created = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="flowchart-process",
        page=0,
        x=1000,
        y=1000,
        width=8000,
        height=4000,
    )
    assert created.get("status") == "ok", created
    filled = FillDrawFields().execute(tctx, page=0, fields=[{"name": created["name"], "value": "Hi"}])
    assert filled.get("status") == "ok", filled
    pos = _svg_text_positions(ctx, page.getByIndex(created["index"]))
    assert len(pos) == 1, pos
    assert 3000 < pos[0][0] < 4500, pos
    assert 1500 < pos[0][1] < 3000, pos


@native_test
@with_native_doc("impress", reuse=False)
def test_placeholder_text_keeps_direct_right_alignment(ctx, doc):
    from plugin.draw.placeholders import SetPlaceholderText

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="impress", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    # Look each shape up once and keep the proxies. Releasing a shape proxy over URP is asynchronous
    # (binaryurp/source/bridge.cxx: Bridge::makeReleaseCall), and a late SvxShape::~SvxShape
    # (svx/source/unodraw/unoshape.cxx) detaches a newer wrapper fetched for the same shape, which
    # GraphicExportFilter.setSourceDocument then rejects.
    shapes = [page.getByIndex(i) for i in range(page.getCount())]
    title_idx = next(i for i, shape in enumerate(shapes) if "TitleTextShape" in shape.getShapeType())
    title = shapes[title_idx]
    width = title.getSize().Width
    edit_res = UpsertShape().execute(tctx, action="edit", page=0, index=title_idx, h_align="right")
    assert edit_res.get("status") == "ok", edit_res

    set_res = SetPlaceholderText().execute(tctx, page=0, role="title", text="RightTitle")
    assert set_res.get("status") == "ok", set_res
    pos = _svg_text_positions(ctx, title)
    assert pos and pos[0][0] > width // 2, (pos, width)


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_custom_shape_text_defaults(ctx, doc):
    _check_custom_shape_text_defaults(ctx, doc, "writer")


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_custom_shape_text_defaults(ctx, doc):
    _check_custom_shape_text_defaults(ctx, doc, "calc")


@native_test
@with_native_doc("draw")
def test_draw_custom_shape_text_defaults(ctx, doc):
    _check_custom_shape_text_defaults(ctx, doc, "draw")


@native_test
@with_native_doc("impress", reuse=False)
def test_impress_custom_shape_text_defaults(ctx, doc):
    _check_custom_shape_text_defaults(ctx, doc, "impress")


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_custom_shape_empty_save_reload_keeps_center(ctx, doc):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    upsert = UpsertShape()
    res = upsert.execute(
        tctx,
        action="create",
        shape_type="flowchart-process",
        page=0,
        x=1000,
        y=1000,
        width=8000,
        height=4000,
    )
    assert res.get("status") == "ok", res

    tmpdir = tempfile.mkdtemp(prefix="shape_reload_")
    reloaded = None
    try:
        out_path = os.path.join(tmpdir, "shape.odg")
        doc.storeToURL(uno.systemPathToFileUrl(out_path), ())
        desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        reloaded = desktop.loadComponentFromURL(
            uno.systemPathToFileUrl(out_path),
            "_blank",
            0,
            (PropertyValue(Name="Hidden", Value=True),),
        )
        tctx2 = ToolContext(doc=reloaded, ctx=ctx, doc_type="draw", services=None, caller="test")
        res_edit = upsert.execute(
            tctx2,
            action="edit",
            index=res["index"],
            page=0,
            text="Hi",
            font_size=18,
        )
        assert res_edit.get("status") == "ok", res_edit
        page2 = DrawBridge(reloaded).get_pages().getByIndex(0)
        pos = _svg_text_positions(ctx, page2.getByIndex(res["index"]))
        assert len(pos) == 1, pos
        assert 3000 < pos[0][0] < 4500, pos
        assert 1500 < pos[0][1] < 3000, pos
    finally:
        if reloaded is not None:
            reloaded.close(True)
        shutil.rmtree(tmpdir, ignore_errors=True)


@native_test
@with_native_doc("impress", reuse=False)
def test_impress_empty_placeholder_h_align_stays_empty(ctx, doc):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="impress", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    assert page.getByIndex(0).getPropertyValue("IsEmptyPresentationObject") is True
    res = UpsertShape().execute(tctx, action="edit", index=0, page=0, h_align="center")
    assert res.get("status") == "ok", res
    assert page.getByIndex(0).getPropertyValue("IsEmptyPresentationObject") is True


@native_test
@with_native_doc("draw")
def test_shape_upsert_wrap_text_off_draw_custom_shape(ctx, doc):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    res = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="flowchart-process",
        page=0,
        x=1000,
        y=1000,
        width=6000,
        height=3000,
        text="Hello world",
        wrap_text=False,
    )
    assert res.get("status") == "ok", res
    shape = page.getByIndex(res["index"])
    assert shape.getPropertyValue("TextWordWrap") is False


@native_test
@with_native_doc("draw")
def test_shape_upsert_arrowheads_line_and_connector(ctx, doc):
    from plugin.draw.tree import build_shape_tree

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    upsert = UpsertShape()

    for shape_type, y in (("line", 1000), ("connector", 3000)):
        created = upsert.execute(
            tctx,
            action="create",
            shape_type=shape_type,
            page=0,
            x=1000,
            y=y,
            width=4000,
            height=0 if shape_type == "line" else 1000,
            line_end="arrow",
            line_start="none",
        )
        assert created.get("status") == "ok", created
        shape = page.getByIndex(created["index"])
        assert shape.getPropertyValue("LineEndName") == "Arrow"
        assert len(shape.getPropertyValue("LineEnd").Coordinates) > 0
        assert shape.getPropertyValue("LineEndWidth") >= 300
        assert shape.getPropertyValue("LineStartName") == ""
        assert len(shape.getPropertyValue("LineStart").Coordinates) == 0

    tree = build_shape_tree(page)
    for node in tree:
        assert node["style"]["LineEndName"] == "Arrow"
        assert "LineStartName" not in node["style"]

    edited = upsert.execute(
        tctx,
        action="edit",
        index=0,
        page=0,
        line_end="none",
        line_start="arrow",
    )
    assert edited.get("status") == "ok", edited
    line_shape = page.getByIndex(0)
    assert line_shape.getPropertyValue("LineEndName") == ""
    assert len(line_shape.getPropertyValue("LineEnd").Coordinates) == 0
    assert line_shape.getPropertyValue("LineStartName") == "Arrow"
    assert len(line_shape.getPropertyValue("LineStart").Coordinates) > 0
    tree_after = build_shape_tree(page)
    assert tree_after[0]["style"]["LineStartName"] == "Arrow"
    assert "LineEndName" not in tree_after[0]["style"]


@native_test
@with_native_doc("draw")
def test_shape_connect_arrowhead_uno(ctx, doc):
    from plugin.draw.shapes import ConnectShapes
    from plugin.draw.tree import build_shape_tree
    from plugin.framework.tool import prepare_tool_call

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    upsert = UpsertShape()
    for x in (1000, 6000):
        res = upsert.execute(
            tctx,
            action="create",
            shape_type="rectangle",
            page=0,
            x=x,
            y=1000,
            width=2000,
            height=1000,
        )
        assert res.get("status") == "ok", res

    connect = ConnectShapes()
    prep = prepare_tool_call(connect, tctx, "shape_connect", {"start": 0, "end": 1, "line_end": "arrow"})
    assert prep.error is None, prep.error
    assert not prep.ignored, prep.ignored
    res_conn = connect.execute(tctx, **prep.kwargs)
    assert res_conn.get("status") == "ok", res_conn

    conn_shape = page.getByIndex(res_conn["index"])
    assert conn_shape.getPropertyValue("LineEndName") == "Arrow"
    assert len(conn_shape.getPropertyValue("LineEnd").Coordinates) > 0
    assert conn_shape.getPropertyValue("LineStartName") == ""
    assert conn_shape.getPropertyValue("StartShape") is not None
    assert conn_shape.getPropertyValue("EndShape") is not None

    tree = build_shape_tree(page)
    conn_node = tree[res_conn["index"]]
    assert conn_node["style"]["LineEndName"] == "Arrow"
    assert "LineStartName" not in conn_node["style"]


@native_test
@with_native_doc("draw")
def test_shape_create_diagram_arrowheads_uno(ctx, doc):
    from plugin.draw.shapes import CreateDiagram
    from plugin.framework.tool import prepare_tool_call

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    tool = CreateDiagram()
    prep = prepare_tool_call(
        tool,
        tctx,
        "shape_create_diagram",
        {
            "layout": "horizontal_flow",
            "nodes": [
                {"id": "a", "text": "A"},
                {"id": "b", "text": "B"},
                {"id": "c", "text": "C"},
            ],
            "connections": [
                {"from": "a", "to": "b", "line_end": "arrow"},
                {"from": "b", "to": "c"},
            ],
        },
    )
    assert prep.error is None, prep.error
    out = tool.execute(tctx, **prep.kwargs)
    assert out.get("status") == "ok", out

    arrow_conn = page.getByIndex(out["connections"][0]["index"])
    assert arrow_conn.getPropertyValue("LineEndName") == "Arrow"
    assert len(arrow_conn.getPropertyValue("LineEnd").Coordinates) > 0
    assert arrow_conn.getPropertyValue("LineStartName") == ""
    node_a = out["nodes"][0]
    assert arrow_conn.EndPosition.X > node_a["x"] + node_a["width"]

    plain_conn = page.getByIndex(out["connections"][1]["index"])
    assert plain_conn.getPropertyValue("LineEndName") == ""
    assert len(plain_conn.getPropertyValue("LineEnd").Coordinates) == 0


@native_test
@with_native_doc("writer", reuse=False)
def test_shape_delete_writer_picture_while_recording_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.images.image_tools import insert_image_at_locator

    insert_image_at_locator(ctx, doc, logo_32_path(), width_mm=15, height_mm=15)
    assert doc.getGraphicObjects().getCount() == 1
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = DeleteShape().execute(tctx, index=0)
        assert res["status"] == "ok"
        assert res["tracked"] is True and res["pending_review"] is True
        assert doc.getRedlines().getCount() >= 1

        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert doc.getGraphicObjects().getCount() == 1

        res2 = DeleteShape().execute(tctx, index=0)
        assert res2["status"] == "ok" and res2["tracked"] is True
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert doc.getGraphicObjects().getCount() == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("draw")
def test_shape_integer_colors_and_nested_arrowhead_uno(ctx, doc):
    from plugin.draw.shapes import CreateDiagram, UpsertShape
    from plugin.framework.tool import prepare_tool_call

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="draw", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)

    upsert = UpsertShape()
    prep_shape = prepare_tool_call(
        upsert,
        tctx,
        "shape_upsert",
        {
            "action": "create",
            "shape_type": "rectangle",
            "x": 1000,
            "y": 1000,
            "width": 2000,
            "height": 1000,
            "line_color": 255,
            "fill_color": 0,
        },
    )
    assert prep_shape.error is None, prep_shape.error
    res_shape = upsert.execute(tctx, **prep_shape.kwargs)
    assert res_shape.get("status") == "ok", res_shape
    shape = page.getByIndex(res_shape["index"])
    assert int(shape.getPropertyValue("LineColor")) == 0x0000FF
    assert int(shape.getPropertyValue("FillColor")) == 0

    diagram = CreateDiagram()
    prep_diag = prepare_tool_call(
        diagram,
        tctx,
        "shape_create_diagram",
        {
            "layout": "horizontal_flow",
            "nodes": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
            "connections": [{"from": "a", "to": "b", "line_end": "Arrow", "line_color": 255}],
        },
    )
    assert prep_diag.error is None, prep_diag.error
    res_diag = diagram.execute(tctx, **prep_diag.kwargs)
    assert res_diag.get("status") == "ok", res_diag
    conn = page.getByIndex(res_diag["connections"][0]["index"])
    assert conn.getPropertyValue("LineEndName") == "Arrow"
    assert int(conn.getPropertyValue("LineColor")) == 0x0000FF


def _check_group_index_and_duplicates(ctx, doc, doc_type):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    for i, name in enumerate(("A", "B", "C", "D")):
        created = UpsertShape().execute(
            tctx, action="create", shape_type="rectangle", page=0, name=name, x=1000 + 3000 * i, y=1000, width=2000, height=1500
        )
        assert created["status"] == "ok", created

    for refs in ([0, 0], [0, "0"], [0, "A"]):
        rejected = GroupShapes().execute(tctx, indices=refs, page=0)
        assert rejected["status"] == "error", (refs, rejected)
    aligned = AlignShapes().execute(tctx, indices=[1, "B"], alignment="left", page=0)
    assert aligned["status"] == "error", aligned
    spread = DistributeShapes().execute(tctx, indices=[0, 1, "A"], axis="horizontal", page=0)
    assert spread["status"] == "error", spread
    assert [page.getByIndex(i).Name for i in range(page.getCount())] == ["A", "B", "C", "D"]

    result = GroupShapes().execute(tctx, indices=["C", 0], page=0)
    assert result["status"] == "ok", result
    assert result["index"] == 1, result
    group = page.getByIndex(result["index"])
    assert "GroupShape" in group.getShapeType()
    assert group.Name == result["name"]
    assert sorted(group.getByIndex(i).Name for i in range(group.getCount())) == ["A", "C"]
    # An edit by the returned index must reach the group and leave D, the last shape, alone.
    renamed = UpsertShape().execute(tctx, action="edit", index=result["index"], name="Flag", page=0)
    assert renamed["status"] == "ok", renamed
    assert [page.getByIndex(i).Name for i in range(page.getCount())] == ["B", "Flag", "D"]


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_group_index_and_duplicates(ctx, doc):
    _check_group_index_and_duplicates(ctx, doc, "draw")


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_group_index_and_duplicates(ctx, doc):
    _check_group_index_and_duplicates(ctx, doc, "writer")


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_group_index_and_duplicates(ctx, doc):
    _check_group_index_and_duplicates(ctx, doc, "calc")


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_group_of_text_frames_is_an_error(ctx, doc):
    """Writer's group() skips text frames and returns None when nothing else was passed."""
    text = doc.getText()
    for _idx in range(2):
        frame = doc.createInstance("com.sun.star.text.TextFrame")
        frame.setSize(uno.createUnoStruct("com.sun.star.awt.Size", 3000, 2000))
        text.insertTextContent(text.getStart(), frame, False)
    page = doc.getDrawPage()
    before = [page.getByIndex(i).Name for i in range(page.getCount())]
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    result = GroupShapes().execute(tctx, indices=[0, 1], page=0)
    assert result["status"] == "error", result
    assert [page.getByIndex(i).Name for i in range(page.getCount())] == before


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_delete_grouped_text_boxes_uno(ctx, doc):
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_PARAGRAPH
    from tests.writer.docx_objects_fixture import build_grouped_text_boxes

    for anchor in (AT_PARAGRAPH, AS_CHARACTER):
        scratch = doc
        if anchor == AS_CHARACTER:
            desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
            scratch = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
        reloaded, docx_path = build_grouped_text_boxes(ctx, scratch, anchor_type=anchor)
        try:
            tctx = ToolContext(doc=reloaded, ctx=ctx, doc_type="writer", services=None, caller="test")
            page = reloaded.getDrawPage()
            group = page.getByIndex(0)
            frame0 = group.getByIndex(0).getPropertyValue("TextBoxContent").getName()
            frame1 = group.getByIndex(1).getPropertyValue("TextBoxContent").getName()

            child_del = DeleteShape().execute(tctx, index="0.0")
            assert child_del["status"] == "error", child_del
            assert child_del["code"] == "TEXT_BOX_IN_GROUP"
            assert page.getCount() == 1
            assert group.getCount() == 2
            assert group.getByIndex(0).getPropertyValue("TextBox") is True
            assert group.getByIndex(1).getPropertyValue("TextBox") is True
            assert "in box 0" in group.getByIndex(0).getString()
            assert "in box 1" in group.getByIndex(1).getString()
            assert reloaded.getTextFrames().hasByName(frame0)

            before_titles = undo_titles(reloaded)
            assert before_titles is not None
            grp_del = DeleteShape().execute(tctx, index=0)
            assert grp_del["status"] == "ok", grp_del
            assert page.getCount() == 0
            assert not reloaded.getTextFrames().hasByName(frame0)
            assert not reloaded.getTextFrames().hasByName(frame1)
            after_titles = undo_titles(reloaded)
            assert after_titles is not None and len(after_titles) == len(before_titles) + 1

            reloaded.getUndoManager().undo()
            assert page.getCount() == 1
            restored = page.getByIndex(0)
            assert restored.getByIndex(0).getPropertyValue("TextBox") is True
            assert restored.getByIndex(1).getPropertyValue("TextBox") is True
            assert reloaded.getTextFrames().hasByName(frame0)
            assert reloaded.getTextFrames().hasByName(frame1)
            assert "in box 0" in restored.getByIndex(0).getString()
            assert "in box 1" in restored.getByIndex(1).getString()
        finally:
            try:
                reloaded.close(True)
            except Exception:
                pass
            if anchor == AS_CHARACTER:
                try:
                    scratch.close(True)
                except Exception:
                    pass
            if os.path.exists(docx_path):
                os.unlink(docx_path)


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_group_frame_rejected_and_hidden_shape_warning_uno(ctx, doc):
    text = doc.getText()
    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.setSize(uno.createUnoStruct("com.sun.star.awt.Size", 3000, 2000))
    text.insertTextContent(text.getStart(), frame, False)

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    r1 = UpsertShape().execute(tctx, action="create", shape_type="rectangle", x=1000, y=1000, width=2000, height=1000)
    r2 = UpsertShape().execute(tctx, action="create", shape_type="rectangle", x=4000, y=1000, width=2000, height=1000)
    assert r1["status"] == "ok" and r2["status"] == "ok"

    page = doc.getDrawPage()
    before_count = page.getCount()
    rejected = GroupShapes().execute(tctx, indices=[0, r1["index"]], page=0)
    assert rejected["status"] == "error", rejected
    assert rejected["code"] == "INVALID_PARAM"
    assert page.getCount() == before_count

    page.getByIndex(r2["index"]).setPropertyValue("Visible", False)
    grouped = GroupShapes().execute(tctx, indices=[r1["index"], r2["index"]], page=0)
    assert grouped["status"] == "ok", grouped
    assert grouped["message"] == "Grouped 1 shapes."
    assert any("Only 1 of 2 shapes were grouped" in w for w in grouped.get("warnings", []))


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_inline_shape_upsert_x_refused_uno(ctx, doc):
    from com.sun.star.awt import Size
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
    from tests.writer.docx_objects_fixture import build_grouped_text_boxes

    text = doc.getText()
    cur = text.createTextCursor()
    shape = doc.createInstance("com.sun.star.drawing.CustomShape")
    shape.setSize(Size(3000, 2000))
    shape.setPropertyValue("AnchorType", AS_CHARACTER)
    shape.setPropertyValue("TextAutoGrowHeight", False)
    text.insertTextContent(cur, shape, False)
    shape.setString("orig")
    orig_pos = shape.getPosition()

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    err = UpsertShape().execute(tctx, action="edit", index=0, x=orig_pos.X + 3000, text="nope")
    assert err["status"] == "error", err
    assert err["code"] == "INVALID_PARAM"
    assert err["details"]["parameter"] == "x"
    assert shape.getPosition().X == orig_pos.X
    assert shape.getString() == "orig"

    ok_y = UpsertShape().execute(tctx, action="edit", index=0, y=-1000)
    assert ok_y["status"] == "ok", ok_y
    assert shape.getPosition().Y == -1000

    orig_size = shape.getSize()
    orig_pos_y = shape.getPosition().Y
    err_neg_w = UpsertShape().execute(tctx, action="edit", index=0, width=-700, text="nope")
    assert err_neg_w["status"] == "error", err_neg_w
    assert err_neg_w["code"] == "INVALID_PARAM"
    assert err_neg_w["details"]["parameter"] == "width"
    assert shape.getSize().Width == orig_size.Width
    assert shape.getSize().Height == orig_size.Height
    assert shape.getString() == "orig"

    ok_neg_h = UpsertShape().execute(tctx, action="edit", index=0, height=-700)
    assert ok_neg_h["status"] == "ok", ok_neg_h
    assert shape.getSize().Height == 700
    assert shape.getPosition().Y == orig_pos_y - 700

    line = doc.createInstance("com.sun.star.drawing.LineShape")
    line.setSize(Size(2000, 0))
    line.setPropertyValue("AnchorType", AS_CHARACTER)
    text.insertTextContent(text.getEnd(), line, False)
    line_idx = doc.getDrawPage().getCount() - 1
    orig_poly = line.PolyPolygon
    orig_x0, orig_x1 = orig_poly[0][0].X, orig_poly[0][-1].X
    ok_line = UpsertShape().execute(tctx, action="edit", index=line_idx, width=-1500)
    assert ok_line["status"] == "ok", ok_line
    new_poly = line.PolyPolygon
    new_x0, new_x1 = new_poly[0][0].X, new_poly[0][-1].X
    assert (orig_x1 - orig_x0) * (new_x1 - new_x0) < 0

    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    scratch = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
    reloaded, docx_path = build_grouped_text_boxes(ctx, scratch, anchor_type=AS_CHARACTER)
    try:
        gtctx = ToolContext(doc=reloaded, ctx=ctx, doc_type="writer", services=None, caller="test")
        child = reloaded.getDrawPage().getByIndex(0).getByIndex(1)
        orig_x = child.getPosition().X
        ok_child = UpsertShape().execute(gtctx, action="edit", index="0.1", x=orig_x + 2000)
        assert ok_child["status"] == "ok", ok_child
        assert child.getPosition().X != orig_x

        x_before_neg_w = child.getPosition().X
        ok_child_neg_w = UpsertShape().execute(gtctx, action="edit", index="0.1", width=-1500)
        assert ok_child_neg_w["status"] == "ok", ok_child_neg_w
        assert abs(child.getSize().Width - 1500) <= 2
        assert abs((x_before_neg_w - child.getPosition().X) - 1500) <= 5

        child0 = reloaded.getDrawPage().getByIndex(0).getByIndex(0)
        assert child0.getPosition().X != child.getPosition().X
        ok_align = AlignShapes().execute(gtctx, indices=["0.0", "0.1"], alignment="left")
        assert ok_align["status"] == "ok", ok_align
        assert child0.getPosition().X == child.getPosition().X
    finally:
        try:
            reloaded.close(True)
        except Exception:
            pass
        try:
            scratch.close(True)
        except Exception:
            pass
        if os.path.exists(docx_path):
            os.unlink(docx_path)


def _assert_delete_group_member_undo_warning(ctx, doc, doc_type):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    start = page.getCount()
    created_indices = []
    for x in (1000, 4000, 7000):
        created = UpsertShape().execute(
            tctx,
            action="create",
            shape_type="rectangle",
            page=0,
            x=x,
            y=1000,
            width=1500,
            height=1200,
        )
        assert created["status"] == "ok", created
        created_indices.append(created["index"])

    grouped = GroupShapes().execute(tctx, indices=created_indices, page=0)
    assert grouped["status"] == "ok", grouped
    group_idx = grouped["index"]
    group = page.getByIndex(group_idx)
    assert group.getCount() == 3

    titles_before = undo_titles(doc)
    res_member = DeleteShape().execute(tctx, index=f"{group_idx}.1", page=0)
    assert res_member["status"] == "ok", res_member
    assert group.getCount() == 2
    assert undo_titles(doc) == titles_before
    assert "warning" in res_member

    titles_before_group = undo_titles(doc)
    res_group = DeleteShape().execute(tctx, index=group_idx, page=0)
    assert res_group["status"] == "ok", res_group
    assert "warning" not in res_group
    titles_after_group = undo_titles(doc)
    assert len(titles_after_group) == len(titles_before_group) + 1
    assert page.getCount() == start

    doc.getUndoManager().undo()
    assert page.getCount() == start + 1
    restored_group = page.getByIndex(group_idx)
    assert restored_group.getCount() == 2


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_delete_group_member_undo_warning_uno(ctx, doc):
    _assert_delete_group_member_undo_warning(ctx, doc, "writer")


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_delete_group_member_undo_warning_uno(ctx, doc):
    _assert_delete_group_member_undo_warning(ctx, doc, "draw")


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_delete_group_member_undo_warning_uno(ctx, doc):
    _assert_delete_group_member_undo_warning(ctx, doc, "calc")


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_delete_group_member_while_recording_uno(ctx, doc):
    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    page = DrawBridge(doc).get_pages().getByIndex(0)
    created_indices = []
    for x in (1000, 4000, 7000):
        created = UpsertShape().execute(
            tctx,
            action="create",
            shape_type="rectangle",
            page=0,
            x=x,
            y=1000,
            width=1500,
            height=1200,
        )
        assert created["status"] == "ok", created
        created_indices.append(created["index"])

    grouped = GroupShapes().execute(tctx, indices=created_indices, page=0)
    assert grouped["status"] == "ok", grouped
    group_idx = grouped["index"]

    solo = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=1000,
        y=4000,
        width=1500,
        height=1200,
    )
    assert solo["status"] == "ok", solo
    solo_idx = solo["index"]

    was_recording = bool(doc.getPropertyValue("RecordChanges"))
    doc.setPropertyValue("RecordChanges", True)
    try:
        res_member = DeleteShape().execute(tctx, index=f"{group_idx}.1", page=0)
        assert res_member["status"] == "ok", res_member
        assert res_member["tracked"] is False
        assert "Ctrl+Z undoes it" not in res_member["warning"]

        count_before_solo_delete = page.getCount()
        res_solo = DeleteShape().execute(tctx, index=solo_idx, page=0)
        assert res_solo["status"] == "ok", res_solo
        assert res_solo["tracked"] is False
        assert page.getCount() == count_before_solo_delete - 1

        doc.getUndoManager().undo()
        assert page.getCount() == count_before_solo_delete
    finally:
        doc.setPropertyValue("RecordChanges", was_recording)


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_inline_shape_align_distribute_uno(ctx, doc):
    from com.sun.star.awt import Size
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

    text = doc.getText()
    text.setString("Hello world ")
    inline_shapes = []
    for _idx in range(2):
        shape = doc.createInstance("com.sun.star.drawing.CustomShape")
        shape.setSize(Size(1500, 1000))
        shape.setPropertyValue("AnchorType", AS_CHARACTER)
        text.insertTextContent(text.getEnd(), shape, False)
        inline_shapes.append(shape)

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="writer", services=None, caller="test")
    rect_res = UpsertShape().execute(
        tctx,
        action="create",
        shape_type="rectangle",
        page=0,
        x=6000,
        y=2000,
        width=2000,
        height=1500,
    )
    assert rect_res["status"] == "ok", rect_res
    page = doc.getDrawPage()
    rect = page.getByIndex(rect_res["index"])
    all_shapes = [*inline_shapes, rect]
    orig_positions = [(s.getPosition().X, s.getPosition().Y) for s in all_shapes]
    indices = [0, 1, rect_res["index"]]

    for alignment in ("left", "center_horizontal", "right"):
        res = AlignShapes().execute(tctx, indices=indices, alignment=alignment, page=0)
        assert res["status"] == "error", res
        assert res["code"] == "INVALID_PARAM"
        assert res["details"]["parameter"] == "indices"
        assert [(s.getPosition().X, s.getPosition().Y) for s in all_shapes] == orig_positions

    res_dist = DistributeShapes().execute(tctx, indices=indices, axis="horizontal", page=0)
    assert res_dist["status"] == "error", res_dist
    assert res_dist["code"] == "INVALID_PARAM"
    assert res_dist["details"]["parameter"] == "indices"
    assert [(s.getPosition().X, s.getPosition().Y) for s in all_shapes] == orig_positions

    assert UpsertShape().execute(tctx, action="edit", index=0, y=-500)["status"] == "ok"
    assert UpsertShape().execute(tctx, action="edit", index=1, y=-1200)["status"] == "ok"
    assert inline_shapes[0].getPosition().Y != inline_shapes[1].getPosition().Y
    res_top = AlignShapes().execute(tctx, indices=[0, 1], alignment="top", page=0)
    assert res_top["status"] == "ok", res_top
    assert inline_shapes[0].getPosition().Y == inline_shapes[1].getPosition().Y
