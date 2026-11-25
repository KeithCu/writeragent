# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Draw/Impress shape create (no live soffice)."""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest


from plugin.draw.shapes import (
    AlignShapes,
    ConnectShapes,
    DeleteShape,
    DistributeShapes,
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
        self.Name = ""

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
    """CustomShape Type/engine are set before page.add, and Type is re-applied after add."""
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
        bridge.resolve_page.return_value = (page, 0)
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

    # Post-add re-apply of CustomShapeGeometry expands preset geometry on Draw too.
    assert any(i > add_idx for i in geom_idxs), events
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
    import uno

    shape = MagicMock()
    _clamp_shape_text_autogrow(shape)

    shape.setPropertyValue.assert_any_call("TextAutoGrowHeight", False)
    shape.setPropertyValue.assert_any_call("TextAutoGrowWidth", False)
    shape.setPropertyValue.assert_any_call("TextFitToSize", uno.Enum("com.sun.star.drawing.TextFitToSizeType", "AUTOFIT"))


def test_clamp_shape_text_autogrow_skips_autofit_when_disabled() -> None:
    shape = MagicMock()
    _clamp_shape_text_autogrow(shape, apply_autofit=False)
    names = [c.args[0] for c in shape.setPropertyValue.call_args_list]
    assert names == ["TextAutoGrowHeight", "TextAutoGrowWidth"]


def test_apply_shape_properties_text_defaults_to_autofit() -> None:
    import uno

    shape = MagicMock()
    shape.setString = MagicMock()
    _apply_shape_properties(shape, {"text": "CEO"})

    shape.setString.assert_called_once_with("CEO")
    shape.setPropertyValue.assert_any_call("TextAutoGrowHeight", False)
    shape.setPropertyValue.assert_any_call("TextAutoGrowWidth", False)
    shape.setPropertyValue.assert_any_call("TextFitToSize", uno.Enum("com.sun.star.drawing.TextFitToSizeType", "AUTOFIT"))


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
    Writer and Calc documents run this same Draw ``execute``.
    """
    shapes = [MagicMock(), MagicMock()]
    page = MagicMock()
    page.getByIndex.side_effect = lambda i, shapes=shapes: shapes[i]
    page.getCount.return_value = 2

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

    page.group.return_value.getPropertyValue.return_value = 0
    page.group.return_value.getCount.return_value = 2

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b
    ):
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page.return_value = page
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        result = GroupShapes().execute(tool_ctx, indices=[0, 1])

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

    (created, _geom, _err), shape, page = _make("LineShape", -2890, 0)
    assert created is shape
    page.add.assert_called_once_with(shape)

    from plugin.draw.shapes import DrawError

    for shape_type, width, height in (
        ("LineShape", 0, 0),
        ("RectangleShape", 4000, 0),
        ("RectangleShape", 0, 2500),
        ("RectangleShape", -100, 200),
        ("com.sun.star.drawing.ConnectorShape", 0, 100),
    ):
        try:
            _make(shape_type, width, height)
        except DrawError as exc:
            assert exc.code == "DRAW_INVALID_SIZE"
        else:
            raise AssertionError("%s %s x %s should be invalid" % (shape_type, width, height))


def _valid_draw_pos() -> MagicMock:
    pos = MagicMock()
    pos.X = 100
    pos.Y = 200
    return pos


def _invalid_draw_pos() -> MagicMock:
    pos = MagicMock()
    del pos.X
    return pos


@pytest.mark.parametrize(
    ("page", "pos_factory", "create_result", "expected_code"),
    [
        (None, _valid_draw_pos, MagicMock(), "DRAW_PAGE_NULL"),
        (MagicMock(), _invalid_draw_pos, MagicMock(), "DRAW_INVALID_POSITION"),
        (MagicMock(), _valid_draw_pos, None, "DRAW_SHAPE_CREATION_FAILED"),
    ],
    ids=["null_page", "invalid_position", "creation_returned_none"],
)
def test_safe_create_shape_input_and_creation_errors(page, pos_factory, create_result, expected_code):
    """safe_create_shape returns coded DrawError for null page, bad position, or None shape."""
    from plugin.draw.shapes import DrawError, DrawShapes

    doc = MagicMock()
    doc.createInstance.return_value = create_result
    size = MagicMock()
    size.Width = 300
    size.Height = 400
    with pytest.raises(DrawError) as exc_info:
        DrawShapes().safe_create_shape(doc, page, "RectangleShape", pos_factory(), size)
    assert exc_info.value.code == expected_code


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
        bridge.resolve_page.return_value = (page, 0)
        bridge.get_active_page_index.return_value = 0
        ok = UpsertShape().execute(ctx, action="create", shape_type="line", x=100, y=200, width=4000, height=0)
        rejected = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=100, y=200, width=4000, height=0)
    assert ok["status"] == "ok", ok
    assert rejected["status"] == "error", rejected
    assert rejected["code"] == "DRAW_INVALID_SIZE"
    doc.createInstance.assert_called_once_with("com.sun.star.drawing.LineShape")
    assert ("setSize", 4000, 0) in events


class DisposedException(Exception):
    """Type name matches is_disposal."""


@pytest.mark.parametrize(
    ("tool_cls", "kwargs", "is_disposed", "page_idx"),
    [
        pytest.param(
            UpsertShape,
            {"action": "create", "shape_type": "rectangle", "x": 0, "y": 0, "width": 10, "height": 10, "page": 0},
            True,
            0,
            id="upsert-disposed",
        ),
        pytest.param(
            UpsertShape,
            {"action": "create", "shape_type": "rectangle", "x": 0, "y": 0, "width": 10, "height": 10, "page": 3},
            False,
            3,
            id="upsert-bad-page",
        ),
        pytest.param(GetDrawSummary, {"page": 0}, True, 0, id="summary-disposed"),
        pytest.param(AlignShapes, {"indices": [0, 1], "alignment": "left", "page": 0}, True, 0, id="align-disposed"),
        pytest.param(AlignShapes, {"indices": [0, 1], "alignment": "left", "page": 9}, False, 9, id="align-bad-page"),
    ],
)
def test_shape_tools_propagate_resolve_page_errors(tool_cls, kwargs, is_disposed, page_idx):
    from plugin.framework.errors import ToolExecutionError

    ctx = MagicMock()
    ctx.active_page_index = 0
    exc = DisposedException("gone") if is_disposed else ToolExecutionError(f"Page index {page_idx} out of range.")
    expected_exc = DisposedException if is_disposed else ToolExecutionError
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.side_effect = exc
        with pytest.raises(expected_exc) as exc_info:
            tool_cls().execute(ctx, **kwargs)
    if not is_disposed:
        assert f"Page index {page_idx} out of range." in str(exc_info.value)


@pytest.mark.parametrize(
    ("kwargs", "expected_ok", "err_substrings"),
    [
        pytest.param({"action": "edit"}, False, ("index", "name"), id="edit-missing-target"),
        pytest.param({"action": "edit", "name": "fld_product"}, True, (), id="edit-by-name"),
        pytest.param({"action": "edit", "index": 0}, True, (), id="edit-by-index"),
        pytest.param(
            {"action": "create", "shape_type": "rectangle", "x": "10", "y": 0, "width": 10, "height": 10},
            False,
            ("x",),
            id="create-bad-x",
        ),
        pytest.param(
            {"action": "create", "shape_type": "rectangle", "x": 0, "y": 0, "width": 10, "height": 10, "rotation_angle": "5"},
            False,
            ("rotation_angle",),
            id="create-bad-rotation",
        ),
        pytest.param(
            {"action": "create", "shape_type": "rectangle", "x": 0, "y": 0, "width": 10, "height": 10, "fill_style": "plaid"},
            False,
            ("fill_style",),
            id="create-bad-fill-style",
        ),
        pytest.param(
            {"action": "create", "shape_type": "rectangle", "x": 0, "y": 0, "width": 10, "height": 10, "fill_style": "solid", "rotation_angle": 5},
            True,
            (),
            id="create-valid",
        ),
    ],
)
def test_shape_upsert_validate(kwargs, expected_ok, err_substrings):
    ok, err = UpsertShape().validate(**kwargs)
    assert ok is expected_ok
    if expected_ok:
        assert err is None
    else:
        assert err is not None
        for sub in err_substrings:
            assert sub in err


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
        bridge.resolve_page.return_value = (page, 0)
        bridge.get_active_page_index.return_value = 0
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=0, y=0, width=10, height=10)
    assert out["status"] == "error"
    assert out["code"] == "DRAW_SHAPE_CREATION_ERROR"


def test_shape_group_failure_propagates():
    shapes = [MagicMock(), MagicMock()]
    page = MagicMock()
    page.getByIndex.side_effect = lambda i, shapes=shapes: shapes[i]
    page.getCount.return_value = 2
    page.group.side_effect = RuntimeError("nope")

    collection = MagicMock()
    uno_ctx = MagicMock()
    uno_ctx.ServiceManager.createInstanceWithContext.return_value = collection

    tool_ctx = MagicMock()
    tool_ctx.doc = MagicMock()
    tool_ctx.ctx = uno_ctx

    pages = MagicMock()
    pages.getByIndex.return_value = page

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b
    ):
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.resolve_page.return_value = (page, 0)
        bridge.get_active_page.return_value = page
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
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
        bridge.resolve_page.return_value = (page, 0)
        bridge.get_active_page_index.return_value = 0

        # Off-slide position x=30000 on a 28000 slide
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=30000, y=1000, width=2000, height=2000)

    assert out["status"] == "ok"
    assert "warnings" in out
    assert "Shape is outside the page bounds." in out["warnings"]
    # Invariant: dimensions belong in chat document context, not every tool result.
    assert "page_width" not in out and "width_mm" not in out


def _make_name_test_env(existing_names: list[str], created_name: str = ""):
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    page_shapes = []
    for existing in existing_names:
        s = MagicMock()
        s.Name = existing
        s.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
        s.getPosition.return_value = _Pos(100, 100)
        s.getSize.return_value = _Size(100, 100)
        page_shapes.append(s)

    page.add.side_effect = lambda s: page_shapes.append(s)
    page.getCount.side_effect = lambda: len(page_shapes)
    page.getByIndex.side_effect = lambda i: page_shapes[i]

    created = MagicMock()
    created.Name = created_name
    created.getPosition.return_value = _Pos(100, 100)
    created.getSize.return_value = _Size(200, 200)
    created.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.return_value = created

    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0
    return ctx, doc, page, page_shapes, created


def test_shape_upsert_create_assigns_unused_name():
    ctx, _doc, page, _shapes, created = _make_name_test_env(["Shape1"], created_name="")
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=100, y=100, width=200, height=200)
    assert out["status"] == "ok"
    assert out["name"] == "Shape2"
    assert created.Name == "Shape2"


@pytest.mark.parametrize(
    ("existing_names", "expected_name"),
    [
        (["Shape2", "Shape3"], "Shape4"),
        (["Title", "Shape1a", "Shape\u00b2", "Shape2"], "Shape3"),
        (["Title"], "Shape1"),
    ],
)
def test_shape_upsert_create_uses_max_plus_one_not_deleted_gap(existing_names, expected_name):
    ctx, _doc, page, _shapes, created = _make_name_test_env(existing_names, created_name="")
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=100, y=100, width=200, height=200)
    assert out["status"] == "ok"
    assert out["name"] == expected_name
    assert created.Name == expected_name


def test_shape_upsert_create_keeps_existing_name():
    ctx, _doc, page, _shapes, created = _make_name_test_env(["Shape1"], created_name="Shape7")
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=100, y=100, width=200, height=200)
    assert out["status"] == "ok"
    assert out["name"] == "Shape7"
    assert created.Name == "Shape7"


@pytest.mark.parametrize(
    ("kwargs", "expect_status"),
    [
        ({"action": "create", "shape_type": "rectangle", "x": 100, "y": 100, "width": 200, "height": 200, "name": "Box"}, "error"),
        ({"action": "edit", "index": 0, "x": 999, "name": "Box"}, "error"),
        ({"action": "edit", "index": 0, "name": "First"}, "ok"),
    ],
)
def test_shape_upsert_rejects_taken_name(kwargs, expect_status):
    ctx, doc, page, page_shapes, _created = _make_name_test_env(["First", "Box"])
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = UpsertShape().execute(ctx, **kwargs)
    assert out["status"] == expect_status
    assert page_shapes[0].Name == "First"
    doc.createInstance.assert_not_called()
    if expect_status == "error":
        assert out["code"] == "INVALID_PARAM"
        page_shapes[0].setPosition.assert_not_called()


def test_shape_upsert_edit_unknown_name_lists_available():
    ctx, _doc, page, page_shapes, _created = _make_name_test_env(["Title", "Titl"])
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = UpsertShape().execute(ctx, action="edit", name="Titel", text="x")
    assert out["status"] == "error"
    assert out["details"]["available"] == ["Title", "Titl"]
    assert "Title" in out["details"]["similar"]
    for shape in page_shapes:
        shape.setPosition.assert_not_called()
        shape.setString.assert_not_called()


def test_shape_upsert_create_strips_explicit_name():
    ctx, _doc, page, _shapes, created = _make_name_test_env([], created_name="")
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = UpsertShape().execute(
            ctx,
            action="create",
            shape_type="rectangle",
            x=100,
            y=100,
            width=200,
            height=200,
            name=" Box",
        )
    assert out["status"] == "ok"
    assert out["name"] == "Box"
    assert created.Name == "Box"


def test_shape_group_name_failure_adds_warning():
    ctx, _doc, page, _shapes, _created = _make_name_test_env(["Left", "Right"])

    class _ReadOnlyNameGroup:
        @property
        def Name(self) -> str:
            return ""

        @Name.setter
        def Name(self, _value: str) -> None:
            raise RuntimeError("Name is read-only")

        def getPropertyValue(self, _name: str) -> int:
            return 0

        def getCount(self) -> int:
            return 2

    page.group.return_value = _ReadOnlyNameGroup()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = GroupShapes().execute(ctx, indices=["Left", "Right"])
    assert out["status"] == "ok"
    assert out["index"] == 0
    assert out["name"] == ""
    assert out["warnings"] == ["Failed to assign group name"]


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
        bridge.resolve_page.return_value = (page, 0)
        bridge.get_active_page_index.return_value = 0

        out = UpsertShape().execute(ctx, action="create", shape_type="rectangle", x=1000, y=1000, width=2000, height=2000)

    assert out["status"] == "ok"
    assert "warnings" not in out


def test_outside_page_zero_width_or_height():
    from plugin.draw.shapes import _outside_page
    page = MagicMock(Width=0, Height=1000)
    shape = MagicMock()
    shape.getPosition.return_value = MagicMock(X=10, Y=10)
    shape.getSize.return_value = MagicMock(Width=100, Height=100)
    assert not _outside_page(page, shape)


def test_shape_summary_name():
    tool = GetDrawSummary()

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
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
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
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
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
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        ds_cls.return_value.safe_create_shape.return_value = (shape, True, None)
        out = tool.execute(ctx, nodes=[{"id": "a", "text": "A"}], layout="horizontal_flow")

    assert out["status"] == "ok"
    assert out["layout"] == "horizontal_flow"


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
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
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


def _line_style_only_fail(name: str, _value: object) -> None:
    if name == "LineStyle":
        raise RuntimeError("line style fail")


def _noop_shape(_shape: MagicMock) -> None:
    return None


@pytest.mark.parametrize(
    ("props", "setup_shape", "char_patch", "expected"),
    [
        pytest.param({"text": "x"}, lambda s: setattr(s.setString, "side_effect", RuntimeError("text fail")), None, ["text"], id="text-runtime"),
        pytest.param({"text": "x"}, lambda s: setattr(s.setString, "side_effect", DisposedException("gone")), None, DisposedException, id="text-disposed"),
        pytest.param({"fill_color": "none"}, lambda s: setattr(s.setPropertyValue, "side_effect", RuntimeError("prop fail")), None, ["fill_color"], id="fill-none-runtime"),
        pytest.param({"fill_color": "none"}, lambda s: setattr(s.setPropertyValue, "side_effect", DisposedException("gone")), None, DisposedException, id="fill-none-disposed"),
        pytest.param({"fill_style": "solid"}, lambda s: setattr(s.setPropertyValue, "side_effect", RuntimeError("prop fail")), None, ["fill_style"], id="fill-solid-runtime"),
        pytest.param({"fill_style": "solid"}, lambda s: setattr(s.setPropertyValue, "side_effect", DisposedException("gone")), None, DisposedException, id="fill-solid-disposed"),
        pytest.param({"fill_style": "gradient"}, _noop_shape, None, [], id="fill-gradient-noop"),
        pytest.param({"line_color": "#000000"}, lambda s: setattr(s.setPropertyValue, "side_effect", _line_style_only_fail), None, ["line_style"], id="line-style-runtime"),
        pytest.param({"line_color": "#000000"}, lambda s: setattr(s.setPropertyValue, "side_effect", DisposedException("gone")), None, DisposedException, id="line-disposed"),
        pytest.param({"font_size": 12}, _noop_shape, lambda: patch("plugin.draw.shapes.apply_character_properties", return_value={"CharHeight": False}), ["CharHeight"], id="char-false"),
        pytest.param({"font_size": 12}, _noop_shape, lambda: patch("plugin.draw.shapes.apply_character_properties", side_effect=RuntimeError("chars fail")), ["text_properties"], id="char-runtime"),
        pytest.param({"font_size": 12}, _noop_shape, lambda: patch("plugin.draw.shapes.apply_character_properties", side_effect=DisposedException("gone")), DisposedException, id="char-disposed"),
        pytest.param({"bold": True}, _noop_shape, lambda: patch("plugin.draw.shapes.apply_character_properties", return_value={"CharWeight": False}), ["CharWeight"], id="bold-only-reaches-char-path"),
        pytest.param({"corner_radius": 300}, lambda s: setattr(s.setPropertyValue, "side_effect", RuntimeError("no CornerRadius")), None, ["corner_radius"], id="corner-radius-runtime"),
        pytest.param({"h_align": "center"}, lambda s: setattr(s.setPropertyValue, "side_effect", RuntimeError("x")), None, ["h_align"], id="h-align-runtime"),
        pytest.param({"wrap_text": True}, lambda s: setattr(s.setPropertyValue, "side_effect", DisposedException("gone")), None, DisposedException, id="wrap-disposed"),
    ],
)
def test_apply_shape_properties_failed_prop_arms(props, setup_shape, char_patch, expected) -> None:
    shape = MagicMock()
    shape.FillStyle = True
    shape.LineStyle = True
    setup_shape(shape)

    char_ctx = char_patch() if char_patch is not None else nullcontext()
    raise_ctx = pytest.raises(expected) if isinstance(expected, type) else nullcontext()
    with char_ctx, raise_ctx:
        failed = _apply_shape_properties(shape, props)
        if not isinstance(expected, type):
            assert failed == expected


def test_apply_shape_properties_color_forces_solid_style():
    def _make_shape():
        shape = MagicMock()
        shape.getShapeType.return_value = "com.sun.star.drawing.CustomShape"
        shape.FillStyle = True
        shape.LineStyle = True
        return shape

    with patch("plugin.draw.shapes._drawing_enum", side_effect=lambda enum_name, member: (enum_name, member)):
        s1 = _make_shape()
        assert _apply_shape_properties(s1, {"fill_color": "#FF0000"}) == []
        assert ("FillColor", 0xFF0000) in [c.args for c in s1.setPropertyValue.call_args_list]
        assert ("FillStyle", ("FillStyle", "SOLID")) in [c.args for c in s1.setPropertyValue.call_args_list]

        s2 = _make_shape()
        assert _apply_shape_properties(s2, {"fill_color": "#FF0000", "fill_style": "none"}) == []
        fill_styles = [c.args[1] for c in s2.setPropertyValue.call_args_list if c.args[0] == "FillStyle"]
        assert fill_styles == [("FillStyle", "NONE")]

        s3 = _make_shape()
        assert _apply_shape_properties(s3, {"fill_color": "not_a_color"}) == ["fill_color"]
        assert not any(c.args[0] == "FillStyle" for c in s3.setPropertyValue.call_args_list)

        s4 = _make_shape()
        assert _apply_shape_properties(s4, {"line_color": "#0000FF"}) == []
        assert ("LineColor", 0x0000FF) in [c.args for c in s4.setPropertyValue.call_args_list]
        assert ("LineStyle", ("LineStyle", "SOLID")) in [c.args for c in s4.setPropertyValue.call_args_list]

        s5 = _make_shape()
        s5.getShapeType.return_value = "com.sun.star.drawing.LineShape"
        assert _apply_shape_properties(s5, {"fill_color": "#FF0000"}) == []
        assert ("LineColor", 0xFF0000) in [c.args for c in s5.setPropertyValue.call_args_list]
        assert not any(c.args[0] == "FillStyle" for c in s5.setPropertyValue.call_args_list)


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
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        res = GetDrawSummary().execute(ctx, page=0)

    assert res["status"] == "ok"
    indices = [s["index"] for s in res["shapes"]]
    assert indices == [0, 1, "1.0", "1.1", "1.1.0"]


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
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
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
    ctx.doc.supportsService.return_value = False

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        # Delete of "1.0" calls group.remove(child) and not page.remove
        res_child = DeleteShape().execute(ctx, index="1.0")
        assert res_child["status"] == "ok"
        assert "warning" in res_child
        outer_group.remove.assert_called_once_with(child)
        page.remove.assert_not_called()

        # Delete of 0 calls page.remove(rect)
        res_page = DeleteShape().execute(ctx, index=0)
        assert res_page["status"] == "ok"
        assert "warning" not in res_page
        page.remove.assert_called_once_with(rect)


def test_shape_delete_by_name_and_index_wins():
    named = MagicMock()
    named.Name = "Target"
    named.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    page = MagicMock()
    page.getCount.return_value = 1
    page.getByIndex.return_value = named

    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = False
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = DeleteShape().execute(ctx, index=0, name="Target")
    assert out["status"] == "ok"
    page.remove.assert_called_once_with(named)


def test_shape_delete_unknown_name_hint():
    page = MagicMock()
    page.getCount.return_value = 0

    ctx = MagicMock()
    ctx.doc = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        with patch("plugin.draw.shapes.page_shape_names", return_value=["Title", "Titl"]):
            out = DeleteShape().execute(ctx, name="Titel")
    assert out["status"] == "error"
    assert out["details"]["available"] == ["Title", "Titl"]
    assert "Titl" in out["details"]["similar"]


def test_shape_connect_group_align_distribute_by_name():
    left = MagicMock()
    left.Name = "Left"
    left.getPosition.return_value = MagicMock(X=0, Y=0)
    left.getSize.return_value = MagicMock(Width=100, Height=100)
    left.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    right = MagicMock()
    right.Name = "Right"
    right.getPosition.return_value = MagicMock(X=500, Y=0)
    right.getSize.return_value = MagicMock(Width=100, Height=100)
    right.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    page_shapes = [left, right]
    page = MagicMock()
    page.getCount.side_effect = lambda: len(page_shapes)
    page.getByIndex.side_effect = lambda i: page_shapes[i]

    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = False
    ctx.ctx = MagicMock()
    ctx.active_page_index = 0

    conn_shape = MagicMock()
    conn_shape.Name = ""
    group_shape = MagicMock()
    group_shape.Name = ""
    group_shape.getCount.return_value = 2
    page.group.return_value = group_shape

    def _safe_create(*_args, **_kwargs):
        page_shapes.append(conn_shape)
        return (conn_shape, None, None)

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.draw.shapes.DrawShapes.safe_create_shape",
        side_effect=_safe_create,
    ), patch("plugin.draw.shapes._apply_shape_properties", return_value=[]), patch(
        "plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        conn = ConnectShapes().execute(ctx, start="Left", end="Right")
        assert conn["status"] == "ok"
        assert conn["name"]

        page.group.reset_mock()
        group_out = GroupShapes().execute(ctx, indices=["Left", "Right"])
        assert group_out["status"] == "ok"
        assert group_out["name"]
        assert group_out["name"] != conn["name"]
        page.group.assert_called_once()

        align_out = AlignShapes().execute(ctx, indices=["Left", "Right"], alignment="left")
        assert align_out["status"] == "ok"
        assert align_out["indices"] == [0, 1]


def test_shape_group_nested_ref_does_not_group():
    child = MagicMock()
    child.Name = "Inner"
    child.getShapeType.return_value = "com.sun.star.drawing.TextShape"

    group = MagicMock()
    group.getShapeType.return_value = "com.sun.star.drawing.GroupShape"
    group.getCount.return_value = 1
    group.getByIndex.return_value = child

    top = MagicMock()
    top.Name = "Top"
    top.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    page = MagicMock()
    page.getCount.return_value = 2
    page.getByIndex.side_effect = lambda i: top if i == 0 else group

    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = GroupShapes().execute(ctx, indices=["1.0", "Top"])
    assert out["status"] == "error"
    page.group.assert_not_called()


def _make_fresh_proxy_page() -> tuple[MagicMock, list[MagicMock]]:
    proxies: list[MagicMock] = []

    def _get_by_index(_idx: int) -> MagicMock:
        proxy = MagicMock()
        proxy.Name = "Left"
        proxy.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
        proxies.append(proxy)
        return proxy

    page = MagicMock()
    page.getCount.return_value = 1
    page.getByIndex.side_effect = _get_by_index
    return page, proxies


@pytest.mark.parametrize("refs", [[0, 0], [0, "0"], [0, "Left"]])
def test_shape_group_rejects_duplicate_indices(refs: list[int | str]) -> None:
    page, _proxies = _make_fresh_proxy_page()
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = GroupShapes().execute(ctx, indices=refs)
    assert out["status"] == "error"
    page.group.assert_not_called()


def test_shape_align_rejects_duplicate_indices() -> None:
    page, proxies = _make_fresh_proxy_page()
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = AlignShapes().execute(ctx, indices=[0, "Left"], alignment="left")
    assert out["status"] == "error"
    assert all(p.setPosition.call_count == 0 for p in proxies)


def test_shape_group_null_group_returns_error() -> None:
    ctx, _doc, page, _shapes, _created = _make_name_test_env(["Left", "Right"])
    page.group.return_value = None
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = GroupShapes().execute(ctx, indices=["Left", "Right"])
    assert out["status"] == "error"


def test_apply_shape_properties_sets_fill_and_line_style_via_uno_enum() -> None:
    import uno

    shape = MagicMock()
    shape.FillStyle = True
    shape.LineStyle = True
    shape.LineWidth = True
    shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    failed = _apply_shape_properties(
        shape,
        {"fill_style": "solid", "line_color": "#0000FF", "line_width": 50},
    )
    assert failed == []
    shape.setPropertyValue.assert_any_call("FillStyle", uno.Enum("com.sun.star.drawing.FillStyle", "SOLID"))
    shape.setPropertyValue.assert_any_call("LineStyle", uno.Enum("com.sun.star.drawing.LineStyle", "SOLID"))

    shape.setPropertyValue.reset_mock()
    failed = _apply_shape_properties(shape, {"fill_style": "transparent"})
    assert failed == []
    shape.setPropertyValue.assert_any_call("FillStyle", uno.Enum("com.sun.star.drawing.FillStyle", "NONE"))


def test_shape_upsert_negative_size_create_and_edit() -> None:
    from plugin.draw.shapes import UpsertShape

    rect = MagicMock()
    rect.Name = "Shape 1"
    rect.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    rect.getPosition.return_value = MagicMock(X=1000, Y=2000)
    rect.getSize.return_value = MagicMock(Width=500, Height=600)

    line = MagicMock()
    line.Name = "Shape 2"
    line.getShapeType.return_value = "com.sun.star.drawing.LineShape"
    line.getPosition.return_value = MagicMock(X=1000, Y=2000)
    line.getSize.return_value = MagicMock(Width=500, Height=0)

    page = MagicMock()
    page.getCount.return_value = 0
    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.side_effect = [rect, line]

    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        # Create rectangle with negative width and height: normalized to top-left + abs size.
        res_rect = UpsertShape().execute(
            ctx,
            action="create",
            shape_type="rectangle",
            x=5000,
            y=3000,
            width=-2000,
            height=-1000,
        )
        assert res_rect["status"] == "ok"
        assert (rect.setPosition.call_args[0][0].X, rect.setPosition.call_args[0][0].Y) == (3000, 2000)
        assert (rect.setSize.call_args[0][0].Width, rect.setSize.call_args[0][0].Height) == (2000, 1000)

        # Create line with negative width: keeps start x=5000 and signed width=-2890 for direction.
        res_line = UpsertShape().execute(
            ctx,
            action="create",
            shape_type="line",
            x=5000,
            y=3000,
            width=-2890,
            height=0,
        )
        assert res_line["status"] == "ok"
        assert (line.setPosition.call_args[0][0].X, line.setPosition.call_args[0][0].Y) == (5000, 3000)
        assert (line.setSize.call_args[0][0].Width, line.setSize.call_args[0][0].Height) == (-2890, 0)

        # Edit rectangle with only negative width (no x): shifts existing x=1000 by -400 -> 600.
        page.getCount.return_value = 2
        page.getByIndex.side_effect = lambda i: rect if i == 0 else line
        rect.setPosition.reset_mock()
        rect.setSize.reset_mock()
        res_edit = UpsertShape().execute(ctx, action="edit", index=0, width=-400, text="hi")
        assert res_edit["status"] == "ok"
        assert (rect.setPosition.call_args[0][0].X, rect.setPosition.call_args[0][0].Y) == (600, 2000)
        assert (rect.setSize.call_args[0][0].Width, rect.setSize.call_args[0][0].Height) == (400, 600)


def test_shape_upsert_arrowheads_create_and_edit() -> None:
    from plugin.draw.shapes import UpsertShape

    ok, _err = UpsertShape().validate(
        action="create",
        shape_type="line",
        x=100,
        y=100,
        width=2000,
        height=0,
        line_end="arrow",
        line_start="none",
    )
    assert ok is True

    bad_ok, bad_err = UpsertShape().validate(
        action="create",
        shape_type="line",
        x=100,
        y=100,
        width=2000,
        height=0,
        line_end="invalid_marker",
    )
    assert bad_ok is False
    assert bad_err is not None and "line_end" in bad_err

    line = MagicMock()
    line.Name = "Line 1"
    line.getShapeType.return_value = "com.sun.star.drawing.LineShape"
    line.getPosition.return_value = MagicMock(X=100, Y=100)
    line.getSize.return_value = MagicMock(Width=2000, Height=0)

    page = MagicMock()
    page.getCount.return_value = 0
    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.return_value = line

    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        created = UpsertShape().execute(
            ctx,
            action="create",
            shape_type="line",
            x=100,
            y=100,
            width=2000,
            height=0,
            line_end="arrow",
            line_start="none",
        )
        assert created["status"] == "ok"
        set_calls = [(c.args[0], c.args[1]) for c in line.setPropertyValue.call_args_list]
        assert ("LineStartName", "") in set_calls
        assert ("LineEndName", "Arrow") in set_calls
        assert any(prop == "LineEndWidth" and val >= 300 for prop, val in set_calls)
        assert any(prop == "LineEnd" for prop, _val in set_calls)

        page.getCount.return_value = 1
        page.getByIndex.return_value = line
        line.setPropertyValue.reset_mock()
        line.getPropertyValue.return_value = 200

        edited = UpsertShape().execute(
            ctx,
            action="edit",
            index=0,
            line_end="none",
            line_start="arrow",
        )
        assert edited["status"] == "ok"
        edit_calls = [(c.args[0], c.args[1]) for c in line.setPropertyValue.call_args_list]
        assert ("LineStartName", "Arrow") in edit_calls
        assert ("LineEndName", "") in edit_calls
        assert ("LineStartWidth", 600) in edit_calls

        no_line_props = MagicMock(spec=["Name", "getShapeType", "getPosition", "getSize", "setPosition", "setSize"])
        no_line_props.Name = "NoLine"
        no_line_props.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
        no_line_props.getPosition.return_value = MagicMock(X=100, Y=100)
        no_line_props.getSize.return_value = MagicMock(Width=1000, Height=500)
        page.getByIndex.return_value = no_line_props
        warned = UpsertShape().execute(ctx, action="edit", index=0, line_end="arrow")
        assert warned["status"] == "ok"
        assert any("line_end" in w for w in warned.get("warnings", []))


def test_shape_connect_prepare_keeps_arrowhead_params():
    from types import SimpleNamespace
    from plugin.draw.shapes import ConnectShapes
    from plugin.framework.tool import prepare_tool_call

    prep = prepare_tool_call(
        ConnectShapes(),
        SimpleNamespace(doc_type="draw"),
        "shape_connect",
        {"start": 0, "end": 1, "line_end": "arrow"},
    )
    assert prep.error is None
    assert prep.kwargs["line_end"] == "arrow"
    assert not prep.ignored


def test_apply_shape_properties_zero_black_and_empty_omitted():
    from plugin.draw.shapes import _apply_shape_properties

    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    with patch("plugin.draw.shapes.apply_character_properties", return_value={}) as mock_char:
        failed = _apply_shape_properties(shape, {"fill_color": 0, "line_color": 0, "text_color": 0})
    assert failed == []
    set_calls = [(c.args[0], c.args[1]) for c in shape.setPropertyValue.call_args_list]
    assert ("FillColor", 0) in set_calls
    assert ("LineColor", 0) in set_calls
    assert mock_char.call_args.kwargs["color"] == 0

    shape.setPropertyValue.reset_mock()
    with patch("plugin.draw.shapes.apply_character_properties", return_value={}) as mock_char:
        failed_empty = _apply_shape_properties(shape, {"fill_color": "", "line_color": "", "text_color": ""})
    assert failed_empty == []
    shape.setPropertyValue.assert_not_called()
    mock_char.assert_not_called()


def test_shape_group_rejects_frame_shape_member():
    frame = MagicMock()
    frame.getShapeType.return_value = "FrameShape"
    rect = MagicMock()
    rect.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    shapes = [frame, rect]
    page = MagicMock()
    page.getCount.return_value = 2
    page.getByIndex.side_effect = lambda i: shapes[i]
    ctx = MagicMock()
    ctx.active_page_index = 0
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value = MagicMock()

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = GroupShapes().execute(ctx, indices=[0, 1])

    assert out["status"] == "error"
    assert out["code"] == "INVALID_PARAM"
    assert out["details"]["parameter"] == "indices"
    page.group.assert_not_called()


def test_shape_group_partial_count_adds_warning():
    s0 = MagicMock()
    s0.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    s1 = MagicMock()
    s1.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"
    shapes = [s0, s1]
    page = MagicMock()
    page.getCount.return_value = 2
    page.getByIndex.side_effect = lambda i: shapes[i]
    group = MagicMock()
    group.Name = "Group1"
    group.getPropertyValue.return_value = 0
    group.getCount.return_value = 1
    page.group.return_value = group
    ctx = MagicMock()
    ctx.active_page_index = 0
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value = MagicMock()

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = GroupShapes().execute(ctx, indices=[0, 1])

    assert out["status"] == "ok"
    assert out["message"] == "Grouped 1 shapes."
    assert any("Only 1 of 2 shapes were grouped" in w for w in out["warnings"])


def test_shape_upsert_edit_refuses_x_on_as_character_page_shape():
    from types import SimpleNamespace

    shape = MagicMock()
    shape.Name = "InlineBox"
    shape.getShapeType.return_value = "com.sun.star.drawing.CustomShape"
    shape.getPosition.return_value = SimpleNamespace(X=100, Y=200)
    shape.getSize.return_value = SimpleNamespace(Width=500, Height=600)
    page = MagicMock()
    page.getCount.return_value = 1
    page.getByIndex.return_value = shape
    ctx = MagicMock()
    ctx.active_page_index = 0

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch(
        "plugin.draw.shapes.shape_is_as_character", return_value=True
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = UpsertShape().execute(ctx, action="edit", index=0, x=3000, text="nope")

        assert out["status"] == "error"
        assert out["code"] == "INVALID_PARAM"
        assert out["details"]["parameter"] == "x"
        shape.setPosition.assert_not_called()
        shape.setSize.assert_not_called()
        shape.setString.assert_not_called()

        out_neg_w = UpsertShape().execute(ctx, action="edit", index=0, width=-400, text="nope")
        assert out_neg_w["status"] == "error"
        assert out_neg_w["code"] == "INVALID_PARAM"
        assert out_neg_w["details"]["parameter"] == "width"
        shape.setPosition.assert_not_called()
        shape.setSize.assert_not_called()
        shape.setString.assert_not_called()

        out_both = UpsertShape().execute(ctx, action="edit", index=0, x=3000, width=-400)
        assert out_both["status"] == "error"
        assert out_both["code"] == "INVALID_PARAM"
        assert out_both["details"]["parameter"] == "x"
        shape.setPosition.assert_not_called()
        shape.setSize.assert_not_called()


def test_shape_align_and_distribute_refuse_inline_page_shapes():
    from types import SimpleNamespace

    shapes = []
    for i, (x, y) in enumerate([(0, 100), (3000, 700), (6000, 900)]):
        s = MagicMock()
        s.Name = f"S{i}"
        s.getPosition.return_value = SimpleNamespace(X=x, Y=y)
        s.getSize.return_value = SimpleNamespace(Width=1000, Height=1000)
        shapes.append(s)

    page = MagicMock()
    page.getCount.return_value = len(shapes)
    page.getByIndex.side_effect = lambda i: shapes[i]
    ctx = MagicMock()
    ctx.active_page_index = 0

    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("plugin.draw.shapes.shape_is_as_character", side_effect=lambda s: s is shapes[1]),
        patch("plugin.draw.shapes._outside_page", return_value=False),
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)

        for alignment in ("left", "center_horizontal", "right"):
            res = AlignShapes().execute(ctx, indices=[0, 1, 2], alignment=alignment)
            assert res["status"] == "error"
            assert res["code"] == "INVALID_PARAM"
            assert res["details"]["parameter"] == "indices"
            for s in shapes:
                s.setPosition.assert_not_called()

        res_dist_h = DistributeShapes().execute(ctx, indices=[0, 1, 2], axis="horizontal")
        assert res_dist_h["status"] == "error"
        assert res_dist_h["code"] == "INVALID_PARAM"
        assert res_dist_h["details"]["parameter"] == "indices"
        for s in shapes:
            s.setPosition.assert_not_called()

        res_top = AlignShapes().execute(ctx, indices=[0, 1, 2], alignment="top")
        assert res_top["status"] == "ok"
        shapes[0].setPosition.assert_not_called()
        assert shapes[1].setPosition.call_args.args[0].Y == 100
        assert shapes[2].setPosition.call_args.args[0].Y == 100

        for s in shapes:
            s.setPosition.reset_mock()

        res_dist_v = DistributeShapes().execute(ctx, indices=[0, 1, 2], axis="vertical")
        assert res_dist_v["status"] == "ok"
        shapes[0].setPosition.assert_not_called()
        assert shapes[1].setPosition.call_args.args[0].Y == 500
        shapes[2].setPosition.assert_not_called()
