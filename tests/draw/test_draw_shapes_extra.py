# WriterAgent — unit tests for align/distribute/graphic/diagram tools
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.shapes import AlignShapes, CreateDiagram, DistributeShapes


class _Pos:
    def __init__(self, x, y):
        self.X = x
        self.Y = y


class _Size:
    def __init__(self, w, h):
        self.Width = w
        self.Height = h


class _Shape:
    def __init__(self, x, y, w, h):
        self._pos = _Pos(x, y)
        self._size = _Size(w, h)

    def getPosition(self):
        return self._pos

    def getSize(self):
        return self._size

    def setPosition(self, point):
        self._pos = _Pos(point.X, point.Y)


def test_align_shapes_left():
    shapes = [_Shape(100, 0, 50, 10), _Shape(200, 5, 50, 10)]
    page = MagicMock()
    page.getByIndex.side_effect = lambda i: shapes[i]
    ctx = MagicMock()
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with patch("com.sun.star.awt.Point", lambda x, y: _Pos(x, y), create=True):
            out = AlignShapes().execute(ctx, indices=[0, 1], alignment="left")
    assert out["status"] == "ok"
    assert shapes[0]._pos.X == 100
    assert shapes[1]._pos.X == 100


def test_align_shapes_needs_two():
    out = AlignShapes().execute(MagicMock(), indices=[0], alignment="left")
    assert out["status"] == "error"


def test_distribute_shapes_needs_three():
    out = DistributeShapes().execute(MagicMock(), indices=[0, 1], axis="horizontal")
    assert out["status"] == "error"


def test_create_diagram_reuses_upsert(tmp_path=None):
    ctx = MagicMock()
    ctx.active_page_index = 0
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with patch("plugin.draw.shapes.UpsertShape") as upsert_cls, patch("plugin.draw.shapes.ConnectShapes") as conn_cls:
            upsert_cls.return_value.execute.side_effect = [
                {"status": "ok", "index": 0},
                {"status": "ok", "index": 1},
            ]
            conn_cls.return_value.execute.return_value = {"status": "ok", "index": 2}
            out = CreateDiagram().execute(
                ctx,
                layout="horizontal_flow",
                nodes=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                connections=[{"from": "a", "to": "b"}],
            )
    assert out["status"] == "ok"
    assert out["id_to_index"] == {"a": 0, "b": 1}
    assert len(out["connections"]) == 1


def test_create_diagram_unknown_connection():
    ctx = MagicMock()
    ctx.active_page_index = 0
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with patch("plugin.draw.shapes.UpsertShape") as upsert_cls:
            upsert_cls.return_value.execute.return_value = {"status": "ok", "index": 0}
            out = CreateDiagram().execute(
                ctx,
                nodes=[{"id": "a", "text": "A"}],
                connections=[{"from": "a", "to": "missing"}],
            )
    assert out["status"] == "error"


def test_create_diagram_numeric_ids_pass_connection_check():
    """Up-front validation compares string forms, like id_to_index does."""
    ctx = MagicMock()
    ctx.active_page_index = 0
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with patch("plugin.draw.shapes.UpsertShape") as upsert_cls, patch("plugin.draw.shapes.ConnectShapes") as conn_cls:
            upsert_cls.return_value.execute.side_effect = [
                {"status": "ok", "index": 0},
                {"status": "ok", "index": 1},
            ]
            conn_cls.return_value.execute.return_value = {"status": "ok", "index": 2}
            out = CreateDiagram().execute(
                ctx,
                nodes=[{"id": 1, "text": "A"}, {"id": 2, "text": "B"}],
                connections=[{"from": 1, "to": "2"}],
            )
    assert out["status"] == "ok"
    assert len(out["connections"]) == 1


def test_create_diagram_unknown_connection_creates_nothing():
    ctx = MagicMock()
    ctx.active_page_index = 0
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with patch("plugin.draw.shapes.UpsertShape") as upsert_cls:
            out = CreateDiagram().execute(
                ctx,
                nodes=[{"id": "a", "text": "A"}],
                connections=[{"from": "a", "to": "missing"}],
            )
            upsert_cls.return_value.execute.assert_not_called()
    assert out["status"] == "error"


class DisposedException(Exception):
    """Type name matches is_disposed_exception / is_real_disposal."""


def _align_page(side_effect):
    page = MagicMock()
    page.getByIndex.side_effect = side_effect
    ctx = MagicMock()
    ctx.active_page_index = 0
    return page, ctx


def test_align_shapes_disposed_propagates():
    page, ctx = _align_page(DisposedException("closed"))
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with pytest.raises(DisposedException):
            AlignShapes().execute(ctx, indices=[0, 1], alignment="left")


def test_align_shapes_bad_index_propagates():
    page, ctx = _align_page(IndexError("bad"))
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with pytest.raises(IndexError):
            AlignShapes().execute(ctx, indices=[0, 1], alignment="left")


def test_distribute_shapes_disposed_propagates():
    shapes = [_Shape(0, 0, 10, 10), _Shape(20, 0, 10, 10), _Shape(40, 0, 10, 10)]
    page = MagicMock()

    def get_by_index(i):
        if i == 1:
            raise DisposedException("closed")
        return shapes[i]

    page.getByIndex.side_effect = get_by_index
    ctx = MagicMock()
    ctx.active_page_index = 0
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with pytest.raises(DisposedException):
            DistributeShapes().execute(ctx, indices=[0, 1, 2], axis="horizontal")


def test_create_diagram_connector_failure_removes_connectors():
    ctx = MagicMock()
    ctx.active_page_index = 0
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    shapes = [MagicMock(), MagicMock(), MagicMock()]
    page.getByIndex.side_effect = shapes
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with patch("plugin.draw.shapes.UpsertShape") as upsert_cls, patch("plugin.draw.shapes.ConnectShapes") as conn_cls:
            upsert_cls.return_value.execute.side_effect = [
                {"status": "ok", "index": 0},
                {"status": "ok", "index": 1},
            ]
            conn_cls.return_value.execute.side_effect = [
                {"status": "ok", "index": 2},
                {"status": "error", "message": "connector failed"},
            ]
            out = CreateDiagram().execute(
                ctx,
                layout="horizontal_flow",
                nodes=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                connections=[{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
            )
    assert out["status"] == "error"
    assert [c.args[0] for c in page.remove.call_args_list] == list(reversed(shapes))


def test_create_diagram_cleanup_reraises_disposed():
    ctx = MagicMock()
    ctx.active_page_index = 0
    page = MagicMock()
    page.Width = 28000
    page.Height = 15750
    page.getByIndex.return_value = MagicMock()
    page.remove.side_effect = DisposedException("closed")
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        with patch("plugin.draw.shapes.UpsertShape") as upsert_cls:
            upsert_cls.return_value.execute.side_effect = [
                {"status": "ok", "index": 0},
                {"status": "error", "message": "second node failed"},
            ]
            with pytest.raises(DisposedException):
                CreateDiagram().execute(
                    ctx,
                    nodes=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                )


def test_connect_shapes_failure_removes_connector_and_raises():
    """A failed connect removes the connector, then lets execute_safe report the error."""
    from plugin.draw.shapes import ConnectShapes

    page = MagicMock()
    connector = MagicMock()
    connector.setPropertyValue.side_effect = ValueError("bad end shape")
    with patch("plugin.draw.shapes._mutation_page", return_value=page), patch("plugin.draw.shapes.DrawShapes") as ds_cls:
        ds_cls.return_value.safe_create_shape.return_value = (connector, False, None)
        with pytest.raises(ValueError, match="bad end shape"):
            ConnectShapes().execute(MagicMock(), start=0, end=1)
    page.remove.assert_called_once_with(connector)
