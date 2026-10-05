# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native checks for slide insert index and chat-context page identity."""

import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _exec(doc, ctx, name, args):
    res = TestingFactory.execute_tool(doc, ctx, name, args, doc_type="impress")
    return res if isinstance(res, dict) else json.loads(res)


def _names(pages):
    return [pages.getByIndex(i).Name for i in range(pages.getCount())]


def _rename(page, name):
    page.Name = name


@native_test
@with_native_doc("impress")
def test_add_slide_page_zero_shifts_existing_and_reports_zero(ctx, doc):
    """add_slide(page=0) must occupy index 0 and point follow-up tools there."""
    from com.sun.star.awt import Point, Size

    pages = doc.getDrawPages()
    first = pages.getByIndex(0)
    _rename(first, "KEEP")
    marker = doc.createInstance("com.sun.star.drawing.RectangleShape")
    marker.setPosition(Point(100, 100))
    marker.setSize(Size(1000, 800))
    first.add(marker)
    marker.Name = "MARKER"
    kept_shapes = first.getCount()

    added = _exec(doc, ctx, "add_slide", {"page": 0, "layout": "blank"})
    assert added.get("status") == "ok", added
    assert added.get("active_page_index") == 0, added
    assert pages.getCount() == 2
    assert pages.getByIndex(1).Name == "KEEP"
    assert pages.getByIndex(1).getCount() == kept_shapes
    new_page = pages.getByIndex(0)
    assert new_page.Name != "KEEP"

    def _has_marker(page) -> bool:
        for i in range(page.getCount()):
            shape = page.getByIndex(i)
            if getattr(shape, "Name", "") == "MARKER":
                return True
        return False

    assert _has_marker(pages.getByIndex(1)), "existing slide lost its shape during insert at 0"
    assert not _has_marker(new_page), "new slide at 0 took the previous slide's shape"

    current = doc.getCurrentController().getCurrentPage()
    assert current is not None
    # Follow-up tools use the controller page. It must be the new slide.
    from plugin.framework.uno_context import uno_same

    assert uno_same(current, new_page), "controller stayed on the pre-insert slide"
    shaped = _exec(
        doc,
        ctx,
        "shape_upsert",
        {"action": "create", "shape_type": "rectangle", "x": 200, "y": 200, "width": 1000, "height": 500},
    )
    assert shaped.get("status") == "ok", shaped
    assert shaped.get("page") == 0, shaped
    assert _has_marker(pages.getByIndex(1))
    assert not _has_marker(pages.getByIndex(0))
    assert pages.getByIndex(0).getCount() == shaped.get("shape_count_after")


@native_test
@with_native_doc("impress")
def test_add_slide_middle_index_matches_reported_active_page(ctx, doc):
    pages = doc.getDrawPages()
    _rename(pages.getByIndex(0), "A")
    _exec(doc, ctx, "add_slide", {"layout": "blank"})
    _rename(pages.getByIndex(1), "B")
    _exec(doc, ctx, "add_slide", {"layout": "blank"})
    _rename(pages.getByIndex(2), "C")

    added = _exec(doc, ctx, "add_slide", {"page": 1, "layout": "blank"})
    assert added.get("status") == "ok", added
    assert added.get("active_page_index") == 1, added
    assert _names(pages)[0] == "A"
    assert _names(pages)[2] == "B"
    assert _names(pages)[3] == "C"
    assert _names(pages)[1] not in ("A", "B", "C")
    from plugin.framework.uno_context import uno_same

    current = doc.getCurrentController().getCurrentPage()
    assert uno_same(current, pages.getByIndex(1))


@native_test
@with_native_doc("impress")
def test_insert_slide_from_master_and_transform_use_real_index(ctx, doc):
    """Insert after slide 0 on a two-slide deck must sit at index 1, not the end."""
    from plugin.draw.bridge import DrawBridge
    from plugin.draw.transform_engine import SlideCommandEngine
    from plugin.framework.tool import ToolContext
    from plugin.main import get_services

    pages = doc.getDrawPages()
    _rename(pages.getByIndex(0), "A")
    bridge = DrawBridge(doc)
    _page, idx = bridge.insert_slide_from_master(after_index=0, switch=True)
    assert idx == 1
    _rename(pages.getByIndex(1), "NEW")
    assert _names(pages) == ["A", "NEW"]

    _exec(doc, ctx, "add_slide", {})
    # Deck is [A, NEW, <appended>]. Jump back to A and insert via transform.
    _rename(pages.getByIndex(2), "TAIL")
    bridge.set_current_page_index(0)
    tctx = ToolContext(doc, ctx, "impress", get_services(), "test", active_page_index=0)
    engine = SlideCommandEngine(tctx)
    result = engine.apply({"Transforms": {"SlideCommands": [{"InsertMasterSlide": 0}]}})
    assert result.get("status") == "ok", result
    assert result.get("current_slide") == 1, result
    assert pages.getCount() == 4
    assert _names(pages)[0] == "A"
    assert _names(pages)[2] == "NEW"
    assert _names(pages)[3] == "TAIL"


@native_test
@with_native_doc("impress")
def test_draw_context_active_slide_index_matches_controller(ctx, doc):
    from plugin.draw.bridge import DrawBridge, get_draw_context_for_chat
    from plugin.framework.uno_context import uno_same

    pages = doc.getDrawPages()
    bridge = DrawBridge(doc)
    bridge.create_slide(None, switch=True)
    current = doc.getCurrentController().getCurrentPage()
    real = -1
    for i in range(pages.getCount()):
        if uno_same(pages.getByIndex(i), current):
            real = i
            break
    assert real >= 0
    text = get_draw_context_for_chat(doc, 8000, ctx)
    assert "Active Slide Index: %d" % real in text
    assert "Active Slide Index: -1" not in text
