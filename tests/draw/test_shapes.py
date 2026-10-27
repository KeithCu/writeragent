# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Draw/Impress shape create (no live soffice)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.shapes import (
    AlignShapes,
    DeleteShape,
    GetDrawSummary,
    GroupShapes,
    UpsertShape,
    _ENHANCED_CUSTOM_SHAPE_ENGINE,
    _apply_shape_properties,
    _clamp_shape_text_autogrow,
    _try_writer_anchor_shape_before_add,
    _try_writer_invalidate_and_pump,
)


class _Pos:
    def __init__(self, x, y):
        self.X = x
        self.Y = y


class _Size:
    def __init__(self, w, h):
        self.Width = w
        self.Height = h


class _RecordingShape:
    """Shape stand-in that records property sets for order assertions."""

    def __init__(self, events: list):
        self._events = events
        self._props: dict = {}

    def setPosition(self, position):
        self._events.append(("setPosition", position.X, position.Y))

    def setSize(self, size):
        self._events.append(("setSize", size.Width, size.Height))

    def setPropertyValue(self, name, value):
        self._events.append(("setPropertyValue", name, value))
        self._props[name] = value

    def getPropertyValue(self, name):
        return self._props.get(name)

    def getShapeType(self):
        return "com.sun.star.drawing.CustomShape"

    def getPosition(self):
        return _Pos(1000, 1000)

    def getSize(self):
        return _Size(5000, 5000)

    def getPropertySetInfo(self):
        info = MagicMock()
        info.getProperties.return_value = []
        return info


def _invoke_set_property(obj, method, args):
    """Stand-in for ``uno.invoke(shape, 'setPropertyValue', (name, value))``."""
    getattr(obj, method)(*args)


def test_shape_upsert_octagon_sets_geometry_before_page_add():
    """CustomShape Type/engine must be set before page.add; never again after.

    Post-add CustomShapeGeometry Type=octagon replaces the live SdrRectObj and
    can abort soffice (SfxItemPool::unregisterNameOrIndex).
    """
    events: list = []
    shape = _RecordingShape(events)

    page = MagicMock()
    page_shapes: list = []

    def page_add(added):
        events.append(("page.add",))
        page_shapes.append(added)

    page.add.side_effect = page_add
    page.getCount.side_effect = lambda: len(page_shapes)
    page.getByIndex.side_effect = lambda i: page_shapes[i]

    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page

    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.side_effect = lambda _type: shape

    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0

    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("uno.invoke", side_effect=_invoke_set_property),
        patch("uno.Any", side_effect=lambda _type, value: value),
    ):
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page_index.return_value = 0

        result = UpsertShape().execute(
            ctx,
            action="create",
            shape_type="octagon",
            x=1000,
            y=1000,
            width=5000,
            height=5000,
            fill_color="none",
            line_color="black",
            line_width=100,
        )

    assert result["status"] == "ok"
    assert result["geometry_applied"] is True
    assert "warning" not in result
    doc.createInstance.assert_called_once_with("com.sun.star.drawing.CustomShape")
    page.add.assert_called_once_with(shape)

    add_idx = next(i for i, ev in enumerate(events) if ev[0] == "page.add")
    engine_idxs = [i for i, ev in enumerate(events) if ev[0] == "setPropertyValue" and ev[1] == "CustomShapeEngine"]
    geom_idxs = [i for i, ev in enumerate(events) if ev[0] == "setPropertyValue" and ev[1] == "CustomShapeGeometry"]

    assert engine_idxs, events
    assert geom_idxs, events
    assert engine_idxs[0] < add_idx, events
    assert geom_idxs[0] < add_idx, events
    assert events[engine_idxs[0]][2] == _ENHANCED_CUSTOM_SHAPE_ENGINE
    geom_value = events[geom_idxs[0]][2]
    geom_props = geom_value if isinstance(geom_value, tuple) else (geom_value,)
    assert any(getattr(p, "Name", None) == "Type" and getattr(p, "Value", None) == "octagon" for p in geom_props), events

    # Double-apply after add is the crashy swap; fill/line may still set after add.
    assert not any(i > add_idx for i in engine_idxs), events
    assert not any(i > add_idx for i in geom_idxs), events


def test_shape_upsert_edit_accepts_name_without_index():
    tool = UpsertShape()
    ok, err = tool.validate(action="edit")
    assert not ok
    assert err is not None
    assert "index" in err and "name" in err
    ok, err = tool.validate(action="edit", name="fld_product")
    assert ok
    assert err is None
    ok, err = tool.validate(action="edit", index=0)
    assert ok
    assert err is None


def test_shape_upsert_calc_customshape_reapplies_geometry_after_page_add():
    """Calc SpreadsheetDocument: re-apply EnhancedCustomShapeGeometry after page.add.

    Pre-add Type alone leaves geometry Type-only (no Path) so CustomShapes do not
    paint on the sheet; Writer already re-applies for TextDocument.
    """
    events: list = []
    shape = _RecordingShape(events)

    page = MagicMock()
    page_shapes: list = []

    def page_add(added):
        events.append(("page.add",))
        page_shapes.append(added)

    page.add.side_effect = page_add
    page.getCount.side_effect = lambda: len(page_shapes)
    page.getByIndex.side_effect = lambda i: page_shapes[i]

    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page

    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    doc.supportsService.side_effect = supports
    doc.createInstance.side_effect = lambda _type: shape

    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0

    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("uno.invoke", side_effect=_invoke_set_property),
        patch("uno.Any", side_effect=lambda _type, value: value),
    ):
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page_index.return_value = 0

        result = UpsertShape().execute(
            ctx,
            action="create",
            shape_type="star24",
            x=2000,
            y=5000,
            width=4000,
            height=4000,
            fill_color="blue",
        )

    assert result["status"] == "ok"
    assert result["geometry_applied"] is True

    add_idx = next(i for i, ev in enumerate(events) if ev[0] == "page.add")
    geom_idxs = [i for i, ev in enumerate(events) if ev[0] == "setPropertyValue" and ev[1] == "CustomShapeGeometry"]
    engine_idxs = [i for i, ev in enumerate(events) if ev[0] == "setPropertyValue" and ev[1] == "CustomShapeEngine"]

    assert any(i < add_idx for i in geom_idxs), events
    assert any(i > add_idx for i in geom_idxs), events
    assert any(i < add_idx for i in engine_idxs), events
    assert any(i > add_idx for i in engine_idxs), events


def test_writer_invalidate_does_not_call_process_events_to_idle() -> None:
    """Writer draw-layer invalidate helper must not nest a VCL pump."""
    doc = MagicMock()
    doc.supportsService.return_value = True
    ctrl = MagicMock()
    doc.getCurrentController.return_value = ctrl
    frame = MagicMock()
    ctrl.getFrame.return_value = frame
    win = MagicMock()
    frame.getContainerWindow.return_value = win
    tk = MagicMock()
    win.getToolkit.return_value = tk

    _try_writer_invalidate_and_pump(doc)

    win.invalidate.assert_called_once_with(0)
    tk.processEventsToIdle.assert_not_called()


def test_writer_invalidate_skips_non_writer_doc() -> None:
    doc = MagicMock()
    doc.supportsService.return_value = False

    _try_writer_invalidate_and_pump(doc)

    doc.getCurrentController.assert_not_called()


def test_clamp_shape_text_autogrow_sets_autofit_by_default() -> None:
    """When setting text, AutoGrow stays off and TextFitToSize defaults to AUTOFIT."""
    shape = MagicMock()
    fit = MagicMock()
    fit.AUTOFIT = "AUTOFIT_SENTINEL"
    drawing_mod = MagicMock()
    drawing_mod.TextFitToSizeType = fit
    with patch.dict(
        "sys.modules",
        {
            "com.sun.star.drawing.TextFitToSizeType": fit,
            "com.sun.star.drawing": drawing_mod,
            "com.sun.star": MagicMock(drawing=drawing_mod),
        },
    ):
        _clamp_shape_text_autogrow(shape)

    shape.setPropertyValue.assert_any_call("TextAutoGrowHeight", False)
    shape.setPropertyValue.assert_any_call("TextAutoGrowWidth", False)
    shape.setPropertyValue.assert_any_call("TextFitToSize", "AUTOFIT_SENTINEL")


def test_clamp_shape_text_autogrow_skips_autofit_when_disabled() -> None:
    shape = MagicMock()
    _clamp_shape_text_autogrow(shape, apply_autofit=False)
    names = [c.args[0] for c in shape.setPropertyValue.call_args_list]
    assert names == ["TextAutoGrowHeight", "TextAutoGrowWidth"]


def test_apply_shape_properties_text_defaults_to_autofit() -> None:
    shape = MagicMock()
    shape.setString = MagicMock()
    fit = MagicMock()
    fit.AUTOFIT = "AUTOFIT_SENTINEL"
    drawing_mod = MagicMock()
    drawing_mod.TextFitToSizeType = fit
    with patch.dict(
        "sys.modules",
        {
            "com.sun.star.drawing.TextFitToSizeType": fit,
            "com.sun.star.drawing": drawing_mod,
            "com.sun.star": MagicMock(drawing=drawing_mod),
        },
    ):
        _apply_shape_properties(shape, {"text": "CEO"})

    shape.setString.assert_called_once_with("CEO")
    shape.setPropertyValue.assert_any_call("TextAutoGrowHeight", False)
    shape.setPropertyValue.assert_any_call("TextAutoGrowWidth", False)
    shape.setPropertyValue.assert_any_call("TextFitToSize", "AUTOFIT_SENTINEL")


def test_upsert_shape_font_size_schema_prefers_omit_autofit() -> None:
    """shape_upsert font_size description nudges models to omit for AUTOFIT."""
    desc = UpsertShape.parameters["properties"]["font_size"]["description"]
    assert "Omit" in desc
    assert "AUTOFIT" in desc
    assert "usually correct" in desc


def test_apply_shape_properties_font_size_skips_autofit() -> None:
    """Explicit font_size in schema opts out of TextFitToSize=AUTOFIT."""
    shape = MagicMock()
    shape.setString = MagicMock()
    with patch("plugin.draw.shapes.apply_character_properties") as apply_chars:
        _apply_shape_properties(shape, {"text": "CEO", "font_size": 14})

    shape.setString.assert_called_once_with("CEO")
    names = [c.args[0] for c in shape.setPropertyValue.call_args_list]
    assert "TextAutoGrowHeight" in names
    assert "TextAutoGrowWidth" in names
    assert "TextFitToSize" not in names
    apply_chars.assert_called_once()



def test_shape_group_uses_service_manager_not_document_factory() -> None:
    """ShapeCollection is a global service. Document factories reject it (LO 26).

    Writer and Draw ``createInstance`` raise ServiceNotRegisteredException
    (``unknown service: com.sun.star.drawing.ShapeCollection``). Calc returns None.
    Writer/Calc ``shape_group`` share this Draw ``execute``.
    """
    from plugin.calc.shapes import GroupShapes as CalcGroupShapes
    from plugin.writer.specialized.shapes import GroupShapes as WriterGroupShapes

    for cls in (GroupShapes, WriterGroupShapes, CalcGroupShapes):
        shapes = [MagicMock(), MagicMock()]
        page = MagicMock()
        page.getByIndex.side_effect = lambda i, shapes=shapes: shapes[i]
        page.getCount.return_value = 1

        doc = MagicMock()
        doc.createInstance.side_effect = RuntimeError(
            "unknown service: com.sun.star.drawing.ShapeCollection"
        )

        collection = MagicMock()
        uno_ctx = MagicMock()
        uno_ctx.ServiceManager.createInstanceWithContext.return_value = collection

        tool_ctx = MagicMock()
        tool_ctx.doc = doc
        tool_ctx.ctx = uno_ctx

        pages = MagicMock()
        pages.getByIndex.return_value = page

        with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.get_pages.return_value = pages
            bridge.get_active_page.return_value = page
            bridge_cls.get_slide_for_tool.return_value = page
            result = cls().execute(tool_ctx, indices=[0, 1])

        assert result["status"] == "ok", result
        assert result["index"] == 0
        doc.createInstance.assert_not_called()
        uno_ctx.ServiceManager.createInstanceWithContext.assert_called_once_with(
            "com.sun.star.drawing.ShapeCollection",
            uno_ctx,
        )
        assert [c.args[0] for c in collection.add.call_args_list] == shapes
        page.group.assert_called_once_with(collection)


def test_shape_upsert_line_allows_zero_width_or_height():
    """Axis-aligned LineShape has one zero side. Rectangles still do not."""
    from plugin.draw.shapes import DrawShapes

    draw_shapes = DrawShapes()

    class _Size:
        def __init__(self, width, height):
            self.Width = width
            self.Height = height

    class _Pos:
        def __init__(self):
            self.X = 100
            self.Y = 200

    def _make(shape_type, width, height):
        shape = MagicMock()
        doc = MagicMock()
        doc.createInstance.return_value = shape
        page = MagicMock()
        return draw_shapes.safe_create_shape(doc, page, shape_type, _Pos(), _Size(width, height)), shape, page

    (created, _geom, _err), shape, page = _make("LineShape", 4000, 0)
    assert created is shape
    page.add.assert_called_once_with(shape)
    shape.setSize.assert_called_once()
    assert shape.setSize.call_args[0][0].Height == 0

    (created, _geom, _err), shape, page = _make("com.sun.star.drawing.LineShape", 0, 2500)
    assert created is shape
    page.add.assert_called_once_with(shape)

    from plugin.draw.shapes import DrawError

    for shape_type, width, height in (
        ("LineShape", 0, 0),
        ("LineShape", -1, 100),
        ("RectangleShape", 4000, 0),
        ("RectangleShape", 0, 2500),
        ("com.sun.star.drawing.ConnectorShape", 0, 100),
    ):
        try:
            _make(shape_type, width, height)
        except DrawError as exc:
            assert exc.code == "DRAW_INVALID_SIZE"
        else:
            raise AssertionError("%s %s x %s should be invalid" % (shape_type, width, height))


def test_shape_upsert_execute_line_zero_height():
    events: list = []
    shape = _RecordingShape(events)
    page = MagicMock()
    page_shapes: list = []

    def page_add(added):
        page_shapes.append(added)

    page.add.side_effect = page_add
    page.getCount.side_effect = lambda: len(page_shapes)
    page.getByIndex.side_effect = lambda i: page_shapes[i]
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.return_value = shape
    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page_index.return_value = 0
        ok = UpsertShape().execute(ctx, action="create", shape_type="line", x=100, y=200, width=4000, height=0)
        rejected = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=100, y=200, width=4000, height=0)
    assert ok["status"] == "ok", ok
    assert rejected["status"] == "error", rejected
    assert rejected["code"] == "DRAW_INVALID_SIZE"
    doc.createInstance.assert_called_once_with("com.sun.star.drawing.LineShape")
    assert ("setSize", 4000, 0) in events


class DisposedException(Exception):
    """Type name matches is_disposed_exception."""


def test_shape_upsert_disposed_page_is_not_invalid_index():
    ctx = MagicMock()
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.side_effect = DisposedException("gone")
        with pytest.raises(DisposedException):
            UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=0, y=0, width=10, height=10, page=0)


def test_shape_upsert_bad_page_stays_invalid_index():
    ctx = MagicMock()
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.side_effect = IndexError("bad")
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=0, y=0, width=10, height=10, page=3)
    assert out["status"] == "error"
    assert "Invalid page index" in out["message"]


def test_shape_summary_disposed_page_is_not_missing_page():
    ctx = MagicMock()
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.side_effect = DisposedException("gone")
        with pytest.raises(DisposedException):
            GetDrawSummary().execute(ctx, page=0)


def test_align_shapes_disposed_page_is_not_invalid_index():
    ctx = MagicMock()
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.side_effect = DisposedException("gone")
        with pytest.raises(DisposedException):
            AlignShapes().execute(ctx, indices=[0, 1], alignment="left", page=0)


def test_align_shapes_bad_page_stays_invalid_index():
    ctx = MagicMock()
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.side_effect = IndexError("bad")
        out = AlignShapes().execute(ctx, indices=[0, 1], alignment="left", page=9)
    assert out["status"] == "error"
    assert "Invalid page index" in out["message"]


def test_shape_upsert_validate_rejects_bad_types_and_enum():
    tool = UpsertShape()
    ok, err = tool.validate(action="create", shape_type="rectangle", x="10", y=0, width=10, height=10)
    assert not ok
    assert err is not None and "x" in err

    ok, err = tool.validate(action="create", shape_type="rectangle", x=0, y=0, width=10, height=10, rotation_angle="5")
    assert not ok
    assert err is not None and "rotation_angle" in err

    ok, err = tool.validate(action="create", shape_type="rectangle", x=0, y=0, width=10, height=10, fill_style="plaid")
    assert not ok
    assert err is not None and "fill_style" in err

    ok, err = tool.validate(action="create", shape_type="rectangle", x=0, y=0, width=10, height=10, fill_style="solid", rotation_angle=5)
    assert ok
    assert err is None


def test_shape_upsert_creation_failure_keeps_draw_error_code():
    """createInstance failure must stay DRAW_SHAPE_CREATION_ERROR, not TOOL_EXECUTION_ERROR."""
    page = MagicMock()
    pages = MagicMock()
    pages.getByIndex.return_value = page
    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.side_effect = RuntimeError("uno failed")
    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page_index.return_value = 0
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=0, y=0, width=10, height=10)
    assert out["status"] == "error"
    assert out["code"] == "DRAW_SHAPE_CREATION_ERROR"


def test_shape_group_failure_propagates():
    shapes = [MagicMock(), MagicMock()]
    page = MagicMock()
    page.getByIndex.side_effect = lambda i, shapes=shapes: shapes[i]
    page.group.side_effect = RuntimeError("nope")

    collection = MagicMock()
    uno_ctx = MagicMock()
    uno_ctx.ServiceManager.createInstanceWithContext.return_value = collection

    tool_ctx = MagicMock()
    tool_ctx.doc = MagicMock()
    tool_ctx.ctx = uno_ctx

    pages = MagicMock()
    pages.getByIndex.return_value = page

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page.return_value = page
        bridge_cls.get_slide_for_tool.return_value = page
        with pytest.raises(RuntimeError):
            GroupShapes().execute(tool_ctx, indices=[0, 1])


def test_shape_upsert_off_page_adds_warning():
    """Shapes positioned outside page bounds return a warning without failing."""
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    page_shapes = []

    page.add.side_effect = lambda s: page_shapes.append(s)
    page.getCount.side_effect = lambda: len(page_shapes)
    page.getByIndex.side_effect = lambda i: page_shapes[i]

    shape = MagicMock()
    shape.getPosition.return_value = _Pos(30000, 1000)
    shape.getSize.return_value = _Size(2000, 2000)
    shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page

    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.return_value = shape

    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page_index.return_value = 0

        # Off-slide position x=30000 on a 28000 slide
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=30000, y=1000, width=2000, height=2000)

    assert out["status"] == "ok"
    assert "warnings" in out
    assert "Shape is outside the page bounds." in out["warnings"]
    # Invariant: dimensions belong in chat document context, not every tool result.
    assert "page_width" not in out and "width_mm" not in out


def test_shape_upsert_within_page_has_no_warning():
    """Shapes within page bounds do not trigger the outside bounds warning."""
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    page_shapes = []

    page.add.side_effect = lambda s: page_shapes.append(s)
    page.getCount.side_effect = lambda: len(page_shapes)
    page.getByIndex.side_effect = lambda i: page_shapes[i]

    shape = MagicMock()
    shape.getPosition.return_value = _Pos(1000, 1000)
    shape.getSize.return_value = _Size(2000, 2000)
    shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page

    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.return_value = shape

    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page_index.return_value = 0

        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=1000, y=1000, width=2000, height=2000)

    assert out["status"] == "ok"
    assert "warnings" not in out


def test_shape_tool_service_lists_and_scoping():
    """Draw, Calc, and Writer shape tools share uno_services and doc_types; bases supply core dependencies."""
    from plugin.calc import shapes as calc_shapes
    from plugin.doc.visual_helpers import SHAPE_TOOL_UNO_SERVICES
    from plugin.draw.base import ToolDrawShapeBase
    from plugin.draw import shapes as draw_shapes
    from plugin.writer.specialized import shapes as writer_shapes

    expected_services = list(SHAPE_TOOL_UNO_SERVICES)
    expected_doc_types = ["writer", "calc", "draw", "impress"]
    tool_names = ["UpsertShape", "DeleteShape", "GetDrawSummary", "ConnectShapes", "GroupShapes"]

    for mod in (draw_shapes, calc_shapes, writer_shapes):
        for name in tool_names:
            cls = getattr(mod, name)
            assert cls.uno_services == expected_services
            assert cls.doc_types == expected_doc_types

    calc_core = frozenset({"get_sheet_summary", "read_cell_range"})
    for name in tool_names:
        assert getattr(calc_shapes, name).required_core_tools == calc_core

    writer_core = frozenset({"get_document_content", "get_document_tree"})
    for name in tool_names:
        assert getattr(writer_shapes, name).required_core_tools == writer_core

    assert draw_shapes.AlignShapes.uno_services == ToolDrawShapeBase.uno_services


def test_outside_page_zero_width_or_height():
    from plugin.draw.shapes import _outside_page
    page = MagicMock(Width=0, Height=1000)
    shape = MagicMock()
    shape.getPosition.return_value = MagicMock(X=10, Y=10)
    shape.getSize.return_value = MagicMock(Width=100, Height=100)
    assert not _outside_page(page, shape)


def test_shape_summary_intent_and_name():
    tool = GetDrawSummary()
    assert tool.intent == "read"

    ctx = MagicMock()
    ctx.active_page_index = 0
    page = MagicMock()
    page.getCount.return_value = 1
    shape = MagicMock()
    shape.Name = "Box1"
    shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    shape.getPosition.return_value = MagicMock(X=10, Y=20)
    shape.getSize.return_value = MagicMock(Width=100, Height=200)
    shape.getString.return_value = "Hello"
    page.getByIndex.return_value = shape

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        out = tool.execute(ctx, page=0)

    assert out["status"] == "ok"
    assert out["shapes"][0]["name"] == "Box1"


def test_upsert_shape_invalid_fill_color_adds_warning():
    ctx = MagicMock()
    ctx.doc.supportsService.return_value = False
    ctx.active_page_index = 0
    page = MagicMock()
    page.getCount.return_value = 1
    page.Width = 10000
    page.Height = 10000
    shape = MagicMock()
    shape.getPosition.return_value = MagicMock(X=100, Y=100)
    shape.getSize.return_value = MagicMock(Width=100, Height=100)

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, \
         patch("plugin.draw.shapes.DrawShapes") as ds_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        ds_cls.return_value.safe_create_shape.return_value = (shape, True, None)
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=100, y=100, width=100, height=100, fill_color="not_a_color")

    assert out["status"] == "ok"
    assert any("fill_color" in w for w in out.get("warnings", []))


def test_create_diagram_node_id_uniqueness():
    from plugin.draw.shapes import CreateDiagram
    tool = CreateDiagram()
    ctx = MagicMock()
    # 1 and "1" collide
    out = tool.execute(ctx, nodes=[{"id": 1}, {"id": "1"}])
    assert out["status"] == "error"
    assert "Node ids must be unique." in out["message"]


def test_create_diagram_returns_layout_str():
    from plugin.draw.shapes import CreateDiagram
    tool = CreateDiagram()
    ctx = MagicMock()
    ctx.doc.supportsService.return_value = False
    ctx.active_page_index = 0
    page = MagicMock(Width=10000, Height=10000)
    page.getCount.return_value = 1
    shape = MagicMock()
    shape.getPosition.return_value = MagicMock(X=100, Y=100)
    shape.getSize.return_value = MagicMock(Width=100, Height=100)

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, \
         patch("plugin.draw.shapes.DrawShapes") as ds_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        ds_cls.return_value.safe_create_shape.return_value = (shape, True, None)
        out = tool.execute(ctx, nodes=[{"id": "a", "text": "A"}], layout="horizontal_flow")

    assert out["status"] == "ok"
    assert out["layout"] == "horizontal_flow"
    assert isinstance(out["layout"], str)


def test_upsert_shape_edit_text_only_does_not_set_text_fit_to_size():
    ctx = MagicMock()
    ctx.active_page_index = 0
    page = MagicMock(Width=10000, Height=10000)
    page.getCount.return_value = 1
    shape = MagicMock()
    shape.getPosition.return_value = MagicMock(X=100, Y=100)
    shape.getSize.return_value = MagicMock(Width=100, Height=100)
    page.getByIndex.return_value = shape

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        out = UpsertShape().execute(ctx, action="edit", index=0, text="New text")

    assert out["status"] == "ok"
    shape.setString.assert_called_once_with("New text")
    for call in shape.setPropertyValue.call_args_list:
        assert call[0][0] != "TextFitToSize"


def test_apply_shape_properties_disposal_on_text_propagates():
    shape = MagicMock()
    shape.setString.side_effect = DisposedException("gone")
    with pytest.raises(DisposedException):
        _apply_shape_properties(shape, {"text": "x"})


def test_apply_shape_properties_disposal_on_fill_color_propagates():
    shape = MagicMock()
    shape.setPropertyValue.side_effect = DisposedException("gone")
    with pytest.raises(DisposedException):
        _apply_shape_properties(shape, {"fill_color": "#FF0000"})


def test_apply_shape_properties_non_disposal_still_warns():
    shape = MagicMock()
    shape.setString.side_effect = RuntimeError("boom")
    assert _apply_shape_properties(shape, {"text": "x"}) == ["text"]


def test_clamp_shape_text_autogrow_disposal_propagates():
    shape = MagicMock()
    shape.setPropertyValue.side_effect = DisposedException("gone")
    with pytest.raises(DisposedException):
        _clamp_shape_text_autogrow(shape)


def test_writer_invalidate_disposal_propagates():
    doc = MagicMock()
    doc.supportsService.return_value = True
    doc.getCurrentController.side_effect = DisposedException("gone")
    with pytest.raises(DisposedException):
        _try_writer_invalidate_and_pump(doc)


def test_writer_anchor_disposal_propagates():
    doc = MagicMock()
    doc.supportsService.side_effect = DisposedException("gone")
    with pytest.raises(DisposedException):
        _try_writer_anchor_shape_before_add(doc, MagicMock())


def test_draw_summary_nested_group_shapes():
    rect = MagicMock()
    rect.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    rect.Name = "Rect"
    rect.getPosition.return_value = _Pos(0, 0)
    rect.getSize.return_value = _Size(10, 10)
    rect.getString.return_value = "rect text"

    child = MagicMock()
    child.getShapeType.return_value = "com.sun.star.drawing.TextShape"
    child.Name = "Child"
    child.getPosition.return_value = _Pos(10, 20)
    child.getSize.return_value = _Size(30, 40)
    child.getString.return_value = "child text"

    grandchild = MagicMock()
    grandchild.getShapeType.return_value = "com.sun.star.drawing.CustomShape"
    grandchild.Name = "Grandchild"
    grandchild.getPosition.return_value = _Pos(50, 60)
    grandchild.getSize.return_value = _Size(70, 80)
    grandchild.getString.return_value = "grandchild text"

    inner_group = MagicMock()
    inner_group.getShapeType.return_value = "com.sun.star.drawing.GroupShape"
    inner_group.Name = "InnerGroup"
    inner_group.getPosition.return_value = _Pos(5, 5)
    inner_group.getSize.return_value = _Size(100, 100)
    inner_group.getCount.return_value = 1
    inner_group.getByIndex.side_effect = lambda i: grandchild if i == 0 else None

    outer_group = MagicMock()
    outer_group.getShapeType.return_value = "com.sun.star.drawing.GroupShape"
    outer_group.Name = "OuterGroup"
    outer_group.getPosition.return_value = _Pos(0, 0)
    outer_group.getSize.return_value = _Size(200, 200)
    outer_group.getCount.return_value = 2
    outer_group.getByIndex.side_effect = lambda i: child if i == 0 else (inner_group if i == 1 else None)

    page = MagicMock()
    page.getCount.return_value = 2
    page.getByIndex.side_effect = lambda i: rect if i == 0 else (outer_group if i == 1 else None)

    ctx = MagicMock()
    ctx.doc = MagicMock()
    with patch("plugin.draw.bridge.resolve_shape_page", return_value=(page, 0, None)):
        res = GetDrawSummary().execute(ctx, page=0)

    assert res["status"] == "ok"
    indices = [s["index"] for s in res["shapes"]]
    assert indices == [0, 1, "1.0", "1.1", "1.1.0"]
    assert isinstance(indices[0], int)
    assert isinstance(indices[1], int)
    assert isinstance(indices[2], str)
    assert isinstance(indices[3], str)
    assert isinstance(indices[4], str)


def test_upsert_shape_validate_dotted_index():
    tool = UpsertShape()
    ok, err = tool.validate(action="edit", index="2.0.1")
    assert ok is True
    assert err is None

    ok_float, err_float = tool.validate(action="edit", index=2.5)
    assert ok_float is False
    assert err_float is not None


def test_upsert_shape_edit_nested_group_child():
    grandchild = MagicMock()
    grandchild.getShapeType.return_value = "com.sun.star.drawing.TextShape"
    grandchild.getPosition.return_value = _Pos(0, 0)
    grandchild.getSize.return_value = _Size(10, 10)
    grandchild.Name = "star"

    inner_group = MagicMock()
    inner_group.getShapeType.return_value = "com.sun.star.drawing.GroupShape"
    inner_group.getCount.return_value = 1
    inner_group.getByIndex.side_effect = lambda i: grandchild if i == 0 else None

    child = MagicMock()
    child.getShapeType.return_value = "com.sun.star.drawing.TextShape"

    outer_group = MagicMock()
    outer_group.getShapeType.return_value = "com.sun.star.drawing.GroupShape"
    outer_group.getCount.return_value = 2
    outer_group.getByIndex.side_effect = lambda i: child if i == 0 else (inner_group if i == 1 else None)

    rect = MagicMock()
    rect.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    page = MagicMock()
    page.getCount.return_value = 2
    page.getByIndex.side_effect = lambda i: rect if i == 0 else (outer_group if i == 1 else None)

    ctx = MagicMock()
    ctx.doc = MagicMock()
    with patch("plugin.draw.bridge.resolve_shape_page", return_value=(page, 0, None)):
        result = UpsertShape().execute(ctx, action="edit", index="1.1.0", text="star")

    assert result["status"] == "ok"
    assert result["index"] == "1.1.0"
    grandchild.setString.assert_called_once_with("star")


def test_delete_shape_group_child_and_page_shape():
    child = MagicMock()
    child.getShapeType.return_value = "com.sun.star.drawing.TextShape"

    outer_group = MagicMock()
    outer_group.getShapeType.return_value = "com.sun.star.drawing.GroupShape"
    outer_group.getCount.return_value = 1
    outer_group.getByIndex.side_effect = lambda i: child if i == 0 else None

    rect = MagicMock()
    rect.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    page = MagicMock()
    page.getCount.return_value = 2
    page.getByIndex.side_effect = lambda i: rect if i == 0 else (outer_group if i == 1 else None)

    ctx = MagicMock()
    ctx.doc = MagicMock()

    with patch("plugin.draw.shapes._resolve_mutation_page", return_value=(page, None)):
        # Delete of "1.0" calls group.remove(child) and not page.remove
        res_child = DeleteShape().execute(ctx, index="1.0")
        assert res_child["status"] == "ok"
        outer_group.remove.assert_called_once_with(child)
        page.remove.assert_not_called()

        # Delete of 0 calls page.remove(rect)
        res_page = DeleteShape().execute(ctx, index=0)
        assert res_page["status"] == "ok"
        page.remove.assert_called_once_with(rect)
