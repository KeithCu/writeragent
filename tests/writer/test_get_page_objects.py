# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""get_page_objects must not clone the view cursor through the body XText.

jumpToEndOfPage can land inside a table cell. Body createTextCursorByRange then
raises RuntimeException: End of content node doesn't have the proper start node.
"""
from unittest.mock import MagicMock

from plugin.writer.structural import GetPageObjects, _at_page_anchor_page, _with_left_body_locked


def _empty_named_collection():
    coll = MagicMock()
    coll.getElementNames.return_value = ()
    return coll


def test_scan_page_does_not_clone_via_body_text():
    """Shapes use the view-cursor page, not body createTextCursorByRange at page end."""
    tool = GetPageObjects()
    body = MagicMock()
    body.createTextCursorByRange.side_effect = RuntimeError(
        "End of content node doesn't have the proper start node"
    )
    doc = MagicMock()
    doc.getText.return_value = body
    doc.getGraphicObjects.return_value = _empty_named_collection()
    doc.getTextTables.return_value = _empty_named_collection()
    doc.getTextFrames.return_value = _empty_named_collection()
    draw = MagicMock()
    draw.getCount.return_value = 0
    doc.getDrawPage.return_value = draw

    vc = MagicMock()
    vc.getPage.return_value = 1
    vc.jumpToPage.return_value = True

    result = tool._scan_page(MagicMock(), doc, vc, 1)
    assert result == {"images": [], "tables": [], "frames": [], "shapes": []}
    body.createTextCursorByRange.assert_not_called()
    vc.jumpToEndOfPage.assert_not_called()
    vc.jumpToPage.assert_not_called()


def test_scan_page_includes_paragraph_anchored_shape_on_page(monkeypatch):
    import com.sun.star.text.TextContentAnchorType as anchor_types

    at_para = object()
    monkeypatch.setattr(anchor_types, "AT_PAGE", object())
    monkeypatch.setattr(anchor_types, "AT_PARAGRAPH", at_para)
    monkeypatch.setattr(anchor_types, "AT_CHARACTER", object())
    monkeypatch.setattr(anchor_types, "AS_CHARACTER", object())

    tool = GetPageObjects()
    doc = MagicMock()
    doc.getGraphicObjects.return_value = _empty_named_collection()
    doc.getTextTables.return_value = _empty_named_collection()
    doc.getTextFrames.return_value = _empty_named_collection()

    shape = MagicMock()
    shape.getPropertyValue.side_effect = lambda name: at_para if name == "AnchorType" else 1
    shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    shape.Name = "Box"
    shape.getString.return_value = "label"
    pos = MagicMock(X=1, Y=2)
    size = MagicMock(Width=3, Height=4)
    shape.getPosition.return_value = pos
    shape.getSize.return_value = size

    draw = MagicMock()
    draw.getCount.return_value = 1
    draw.getByIndex.return_value = shape
    doc.getDrawPage.return_value = draw

    vc = MagicMock()
    vc.getPage.return_value = 2

    result = tool._scan_page(MagicMock(), doc, vc, 2)
    assert len(result["shapes"]) == 1
    assert result["shapes"][0]["name"] == "Box"
    vc.gotoRange.assert_called_once()


def test_execute_clones_view_cursor_via_own_text():
    """execute() save/restore must clone via vc.getText(), not the body XText."""
    tool = GetPageObjects()
    body = MagicMock()
    body_start = MagicMock(name="body_start")
    body.getStart.return_value = body_start
    body.createTextCursorByRange.side_effect = RuntimeError(
        "End of content node doesn't have the proper start node"
    )
    own = MagicMock()
    saved = MagicMock(name="saved")
    own.createTextCursorByRange.return_value = saved

    vc = MagicMock()
    vc.getText.return_value = own
    vc.getPage.return_value = 1

    doc = MagicMock()
    doc.getText.return_value = body
    doc.getGraphicObjects.return_value = _empty_named_collection()
    doc.getTextTables.return_value = _empty_named_collection()
    doc.getTextFrames.return_value = _empty_named_collection()
    draw = MagicMock()
    draw.getCount.return_value = 0
    doc.getDrawPage.return_value = draw
    controller = MagicMock()
    controller.getViewCursor.return_value = vc
    doc.getCurrentController.return_value = controller

    ctx = MagicMock()
    ctx.doc = doc
    order = []

    def track_goto(rng, expand):
        if rng is body_start:
            order.append("leave")
        elif rng is saved:
            order.append("restore")

    vc.gotoRange.side_effect = track_goto
    doc.lockControllers.side_effect = lambda: order.append("lock")
    doc.unlockControllers.side_effect = lambda: order.append("unlock")
    result = tool.execute(ctx, page=1)
    assert result.get("status") == "ok"
    body.createTextCursorByRange.assert_not_called()
    own.createTextCursorByRange.assert_called_once_with(vc)
    assert order == ["leave", "lock", "unlock", "restore"]


def test_scan_page_retries_getpage_zero_while_locked():
    """getPage()==0 after a locked table-anchor hop is stale layout, not page 0."""
    tool = GetPageObjects()
    tables = MagicMock()
    tables.getElementNames.return_value = ("Table1",)
    table = MagicMock()
    table.getAnchor.return_value = MagicMock(name="anchor")
    table.getRows.return_value.getCount.return_value = 2
    table.getColumns.return_value.getCount.return_value = 3
    table.getPropertyValue.side_effect = RuntimeError("no AnchorType")
    tables.getByName.return_value = table

    doc = MagicMock()
    doc.getGraphicObjects.return_value = _empty_named_collection()
    doc.getTextTables.return_value = tables
    doc.getTextFrames.return_value = _empty_named_collection()
    draw = MagicMock()
    draw.getCount.return_value = 0
    doc.getDrawPage.return_value = draw
    doc.hasControllersLocked.return_value = True

    pages = [0, 1]
    vc = MagicMock()
    vc.getPage.side_effect = lambda: pages.pop(0) if pages else 1

    result = tool._scan_page(MagicMock(), doc, vc, 1)
    assert result["tables"] == [{"name": "Table1", "rows": 2, "cols": 3}]
    doc.unlockControllers.assert_called_once()
    doc.lockControllers.assert_called_once()


def test_scan_page_skips_paragraph_shape_getpage_zero_without_unlock(monkeypatch):
    """Ordinary paragraph ranges: getPage()==0 is not-on-page, no unlock churn."""
    import com.sun.star.text.TextContentAnchorType as anchor_types

    at_para = object()
    at_page = object()
    monkeypatch.setattr(anchor_types, "AT_PAGE", at_page)
    monkeypatch.setattr(anchor_types, "AT_PARAGRAPH", at_para)
    monkeypatch.setattr(anchor_types, "AT_CHARACTER", object())
    monkeypatch.setattr(anchor_types, "AS_CHARACTER", object())

    tool = GetPageObjects()
    doc = MagicMock()
    doc.getGraphicObjects.return_value = _empty_named_collection()
    doc.getTextTables.return_value = _empty_named_collection()
    doc.getTextFrames.return_value = _empty_named_collection()
    doc.hasControllersLocked.return_value = True

    shape = MagicMock()
    shape.getPropertyValue.side_effect = lambda name: at_para if name == "AnchorType" else 0
    shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    shape.Name = "Box"
    shape.getString.return_value = ""
    draw = MagicMock()
    draw.getCount.return_value = 1
    draw.getByIndex.return_value = shape
    doc.getDrawPage.return_value = draw

    vc = MagicMock()
    vc.getPage.return_value = 0

    result = tool._scan_page(MagicMock(), doc, vc, 1)
    assert result["shapes"] == []
    vc.gotoRange.assert_called_once()
    doc.unlockControllers.assert_not_called()
    doc.lockControllers.assert_not_called()


def test_scan_page_at_page_shape_uses_anchor_page_no_without_view_hop(monkeypatch):
    """AT_PAGE shapes use AnchorPageNo — never _page_at_range / gotoRange."""
    import com.sun.star.text.TextContentAnchorType as anchor_types

    at_page = object()
    monkeypatch.setattr(anchor_types, "AT_PAGE", at_page)
    monkeypatch.setattr(anchor_types, "AT_PARAGRAPH", object())
    monkeypatch.setattr(anchor_types, "AT_CHARACTER", object())
    monkeypatch.setattr(anchor_types, "AS_CHARACTER", object())

    tool = GetPageObjects()
    doc = MagicMock()
    doc.getGraphicObjects.return_value = _empty_named_collection()
    doc.getTextTables.return_value = _empty_named_collection()
    doc.getTextFrames.return_value = _empty_named_collection()

    shape = MagicMock()
    shape.getPropertyValue.side_effect = lambda name: at_page if name == "AnchorType" else 2
    shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    shape.Name = "PageBox"
    shape.getString.return_value = ""
    pos = MagicMock(X=1, Y=2)
    size = MagicMock(Width=3, Height=4)
    shape.getPosition.return_value = pos
    shape.getSize.return_value = size
    draw = MagicMock()
    draw.getCount.return_value = 1
    draw.getByIndex.return_value = shape
    doc.getDrawPage.return_value = draw

    vc = MagicMock()
    result = tool._scan_page(MagicMock(), doc, vc, 2)
    assert len(result["shapes"]) == 1
    assert result["shapes"][0]["name"] == "PageBox"
    vc.gotoRange.assert_not_called()


def test_scan_page_at_page_frame_skips_view_hop(monkeypatch):
    """AT_PAGE frames use AnchorPageNo the same way shapes do."""
    import com.sun.star.text.TextContentAnchorType as anchor_types

    at_page = object()
    monkeypatch.setattr(anchor_types, "AT_PAGE", at_page)

    frames = MagicMock()
    frames.getElementNames.return_value = ("PageFrame",)
    frame = MagicMock()
    frame.getPropertyValue.side_effect = lambda name: {
        "AnchorType": at_page,
        "AnchorPageNo": 3,
        "Size": MagicMock(Width=400, Height=200),
    }[name]
    frames.getByName.return_value = frame

    tool = GetPageObjects()
    doc = MagicMock()
    doc.getGraphicObjects.return_value = _empty_named_collection()
    doc.getTextTables.return_value = _empty_named_collection()
    doc.getTextFrames.return_value = frames
    draw = MagicMock()
    draw.getCount.return_value = 0
    doc.getDrawPage.return_value = draw

    vc = MagicMock()
    result = tool._scan_page(MagicMock(), doc, vc, 3)
    assert result["frames"] == [{"name": "PageFrame", "width_mm": 4, "height_mm": 2}]
    vc.gotoRange.assert_not_called()


def test_at_page_anchor_page_ignores_paragraph_anchor(monkeypatch):
    import com.sun.star.text.TextContentAnchorType as anchor_types

    monkeypatch.setattr(anchor_types, "AT_PAGE", object())
    obj = MagicMock()
    obj.getPropertyValue.side_effect = lambda name: 0 if name == "AnchorPageNo" else object()
    assert _at_page_anchor_page(obj) is None


def test_with_left_body_locked_order_and_restore():
    body_start = MagicMock(name="body_start")
    saved = MagicMock(name="saved")
    own = MagicMock()
    own.createTextCursorByRange.return_value = saved
    vc = MagicMock()
    vc.getText.return_value = own
    body = MagicMock()
    body.getStart.return_value = body_start
    doc = MagicMock()
    doc.getText.return_value = body
    order = []

    def track_goto(rng, expand):
        if rng is body_start:
            order.append("leave")
        elif rng is saved:
            order.append("restore")

    vc.gotoRange.side_effect = track_goto
    doc.lockControllers.side_effect = lambda: order.append("lock")
    doc.unlockControllers.side_effect = lambda: order.append("unlock")

    seen = _with_left_body_locked(doc, vc, lambda: order.append("scan") or "ok")
    assert seen == "ok"
    assert order == ["leave", "lock", "scan", "unlock", "restore"]


def test_execute_skips_lock_when_page_hop_fails():
    """Empty page is valid — a failed hop must not lock (or invent a cell)."""
    tool = GetPageObjects()
    own = MagicMock()
    own.createTextCursorByRange.return_value = MagicMock(name="saved")
    body_start = MagicMock(name="body_start")
    body = MagicMock()
    body.getStart.return_value = body_start
    vc = MagicMock()
    vc.getText.return_value = own

    def fail_leave(rng, expand):
        if rng is body_start:
            raise RuntimeError("cannot leave nested XText")

    vc.gotoRange.side_effect = fail_leave
    doc = MagicMock()
    doc.getText.return_value = body
    doc.getGraphicObjects.return_value = _empty_named_collection()
    doc.getTextTables.return_value = _empty_named_collection()
    doc.getTextFrames.return_value = _empty_named_collection()
    draw = MagicMock()
    draw.getCount.return_value = 0
    doc.getDrawPage.return_value = draw
    doc.getCurrentController.return_value = MagicMock(getViewCursor=MagicMock(return_value=vc))
    ctx = MagicMock()
    ctx.doc = doc
    result = tool.execute(ctx, page=3)
    assert result.get("status") == "ok"
    doc.lockControllers.assert_not_called()
    doc.unlockControllers.assert_not_called()
