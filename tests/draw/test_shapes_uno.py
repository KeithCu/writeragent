# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live check: shape_group builds ShapeCollection via the service manager."""

from plugin.draw.bridge import DrawBridge
from plugin.draw.shapes import GroupShapes, UpsertShape
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
