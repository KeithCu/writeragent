# WriterAgent - tests for run_venv_python_script image handling

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.python.image_egress import (
    _DRAW_GRAPHIC_SERVICE,
    _PLOT_SHAPE_NAME_PREFIX,
    ImageEgressError,
    _shape_anchor_matches_cell,
    insert_image_result_on_sheet,
)
from plugin.tests.testing_utils import CalcCellStub
from plugin.calc.python.venv import RunVenvPythonScript
from plugin.scripting.payload_codec import PAYLOAD_IMAGE

_IMAGE_PAYLOAD = {
    "__wa_payload__": PAYLOAD_IMAGE,
    "format": "svg",
    "data": b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
}


def test_calc_image_result_inserts_on_sheet():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    target = MagicMock(name="target_doc")
    ctx.doc = target

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": _IMAGE_PAYLOAD}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch(
            "plugin.framework.queue_executor.execute_on_main_thread",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ) as main_thread,
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet") as insert,
        patch("plugin.scripting.config_limits.configured_python_max_data_cells", return_value=10000),
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is True
    assert out["image_path"] == "/tmp/plot.svg"
    assert "active sheet" in out["message"]
    assert main_thread.call_count == 1
    insert.assert_called_once_with(ctx.ctx, _IMAGE_PAYLOAD, doc=target, image_index=0)


def test_calc_image_insert_failure_is_not_success():
    """A missing sheet or draw page must not report the plot as inserted."""
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock(name="target_doc")

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": _IMAGE_PAYLOAD}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch(
            "plugin.framework.queue_executor.execute_on_main_thread",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch(
            "plugin.calc.python.image_egress.insert_image_result_on_sheet",
            side_effect=ImageEgressError("target sheet has no DrawPage; image was not inserted"),
        ),
        patch("plugin.scripting.config_limits.configured_python_max_data_cells", return_value=10000),
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "error"
    assert out["image_inserted"] is False
    assert "not inserted" in out["message"]
    assert "active sheet" not in out["message"]


def test_writer_image_result_returns_path_only():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "writer"
    ctx.ctx = MagicMock()

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": _IMAGE_PAYLOAD}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet"),
        patch("plugin.scripting.config_limits.configured_python_max_data_cells", return_value=10000),
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is None
    assert out["image_path"] == "/tmp/plot.svg"


def test_insert_image_result_on_sheet_none_doc_is_failure():
    """A missing document is an insert failure, not a silent success."""
    ctx = MagicMock()
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx") as front_window,
        pytest.raises(ImageEgressError, match="no Calc document"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD)
    # The front window is never used as a fallback.
    front_window.assert_not_called()


def test_insert_image_result_on_sheet_active_sheet_fallback():
    """Fallback to getSheets().getByIndex(0) when getCurrentController is None."""
    ctx = MagicMock()
    doc = MagicMock()
    doc.getCurrentController.return_value = None
    sheet = MagicMock()
    draw_page = MagicMock()
    sheet.DrawPage = draw_page
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    sheets.getByIndex.return_value = sheet
    doc.getSheets.return_value = sheets
    shape = MagicMock()
    doc.createInstance.return_value = shape

    with (
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, doc=doc)

    assert draw_page.add.call_count == 1
    shape.setPropertyValue.assert_called_with("GraphicURL", "file:///tmp/chart.svg")


def test_insert_image_result_on_sheet_background_thread_marshaling():
    """When called off main thread, insert_image_result_on_sheet posts asynchronously to main thread."""
    ctx = MagicMock()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.post_to_main_thread") as post_main,
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD)

    assert post_main.call_count == 1


def test_insert_image_result_on_sheet_uses_passed_doc_not_front_window():
    ctx = MagicMock()
    front = MagicMock(name="front")
    target = MagicMock(name="target")
    sheet = MagicMock()
    cell = MagicMock()
    draw_page = MagicMock()
    sheet.DrawPage = draw_page
    target.getCurrentController.return_value = MagicMock(getActiveSheet=MagicMock(return_value=None))
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=front),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ) as locate,
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
        patch("plugin.calc.calc_utils.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        target.createInstance.return_value = MagicMock()
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target)
    locate.assert_called()
    assert locate.call_args[0][1] is target


def test_insert_image_result_on_sheet_aborts_when_formula_location_fails_for_code():
    """When code is supplied and formula cell location fails, image egress aborts without inserting on active sheet."""
    ctx = MagicMock()
    doc = MagicMock()
    ctrl = MagicMock()
    active_sheet = MagicMock()
    draw_page = MagicMock()
    active_sheet.DrawPage = draw_page
    ctrl.getActiveSheet.return_value = active_sheet
    doc.getCurrentController.return_value = ctrl

    code = "import matplotlib.pyplot as plt; plt.plot([1, 2, 3])"

    with (
        patch("plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc", return_value=None),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp") as mock_write,
        pytest.raises(ImageEgressError, match="could not locate formula cell"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code=code, doc=doc)

    # Must abort before writing temp file or adding shape to active sheet's DrawPage
    mock_write.assert_not_called()
    draw_page.add.assert_not_called()


def test_shape_anchor_matches_by_address_not_identity():
    """Reuse must key off sheet/col/row, not UNO object identity."""
    cell_a = CalcCellStub(col=3, row=6)
    cell_same = CalcCellStub(col=3, row=6)
    cell_other = CalcCellStub(col=0, row=0)
    shape = MagicMock()
    shape.getPropertyValue.return_value = cell_a
    assert _shape_anchor_matches_cell(shape, cell_same)
    assert not _shape_anchor_matches_cell(shape, cell_other)


class _AnchoredShape:
    """Minimal draw shape for reuse checks (service + name + anchor)."""

    def __init__(self, service: str, name: str, anchor: CalcCellStub):
        self.service = service
        self.props: dict[str, object] = {"Name": name, "Anchor": anchor}

    def supportsService(self, service: str) -> bool:
        return service == self.service

    def getPropertyValue(self, key: str):
        return self.props.get(key)

    def setPropertyValue(self, key: str, value: object) -> None:
        self.props[key] = value

    def setSize(self, size: object) -> None:
        self.props["Size"] = size

    def setPosition(self, pos: object) -> None:
        self.props["Position"] = pos


class _DrawPage:
    def __init__(self, shapes: list):
        self.shapes = list(shapes)
        self.added: list = []

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, index: int):
        return self.shapes[index]

    def add(self, shape) -> None:
        self.added.append(shape)
        self.shapes.append(shape)

    def remove(self, shape) -> None:
        if shape in self.shapes:
            self.shapes.remove(shape)


def _insert_at_cell(cell: CalcCellStub, page: _DrawPage, *, doc: MagicMock | None = None, image_index: int = 0) -> MagicMock:
    ctx = MagicMock()
    target = doc or MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
        patch("plugin.calc.calc_utils.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        target.createInstance.return_value = MagicMock()
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=image_index)
    return target


def test_reuse_does_not_clobber_user_image_or_other_shape():
    """A photo or rectangle anchored at the cell is not overwritten."""
    cell = CalcCellStub(col=3, row=6)
    other = CalcCellStub(col=0, row=0)
    user_image = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "Picture 1", cell)
    rectangle = _AnchoredShape("com.sun.star.drawing.RectangleShape", "Box", cell)
    other_plot = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, other)
    page = _DrawPage([user_image, rectangle, other_plot])

    target = _insert_at_cell(cell, page)

    assert "GraphicURL" not in user_image.props
    assert "GraphicURL" not in rectangle.props
    assert "GraphicURL" not in other_plot.props
    target.createInstance.assert_called_once_with(_DRAW_GRAPHIC_SERVICE)
    assert page.added
    page.added[0].setPropertyValue.assert_any_call("GraphicURL", "file:///tmp/chart.svg")
    page.added[0].setPropertyValue.assert_any_call("Name", _PLOT_SHAPE_NAME_PREFIX)


def test_reuse_replaces_named_plot_shape_only():
    """The next plot updates the WriterAgentPlot graphic at that cell."""
    cell = CalcCellStub(col=3, row=6)
    plot = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    user_image = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "Picture 1", cell)
    page = _DrawPage([user_image, plot])

    target = _insert_at_cell(cell, page)

    assert plot.props["GraphicURL"] == "file:///tmp/chart.svg"
    assert "GraphicURL" not in user_image.props
    target.createInstance.assert_not_called()
    assert page.added == []


def test_two_plots_in_single_call_create_distinct_shapes():
    """Two payloads at the same cell produce two shapes with distinct slot names and offsets."""
    cell = CalcCellStub(col=3, row=6)
    page = _DrawPage([])

    shape0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    shape1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", cell)

    def _create_inst(_srv):
        return shape0 if not page.shapes else shape1

    target = MagicMock(name="target")
    target.createInstance.side_effect = _create_inst
    target.getCurrentController.return_value = None

    ctx = MagicMock()
    sheet = MagicMock()
    sheet.DrawPage = page

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", side_effect=["/tmp/chart0.svg", "/tmp/chart1.svg"]),
        patch("uno.systemPathToFileUrl", side_effect=["file:///tmp/chart0.svg", "file:///tmp/chart1.svg"]),
        patch("plugin.calc.calc_utils.get_cell_geometry", return_value=(MagicMock(X=1000, Y=2000), MagicMock(Width=5000, Height=4000))),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0)
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=1)

    assert len(page.shapes) == 2
    assert shape0.props["GraphicURL"] == "file:///tmp/chart0.svg"
    assert shape1.props["GraphicURL"] == "file:///tmp/chart1.svg"
    assert shape0.props["Name"] == _PLOT_SHAPE_NAME_PREFIX
    assert shape1.props["Name"] == f"{_PLOT_SHAPE_NAME_PREFIX}_2"


def test_subsequent_call_replaces_slot_0_and_leaves_slot_1():
    """A subsequent single-image call reuses slot 0 and leaves slot 1 untouched."""
    cell = CalcCellStub(col=3, row=6)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot0.props["GraphicURL"] = "file:///tmp/old0.svg"
    slot1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", cell)
    slot1.props["GraphicURL"] = "file:///tmp/old1.svg"
    page = _DrawPage([slot0, slot1])

    target = _insert_at_cell(cell, page, image_index=0)

    assert slot0.props["GraphicURL"] == "file:///tmp/chart.svg"
    assert slot1.props["GraphicURL"] == "file:///tmp/old1.svg"
    target.createInstance.assert_not_called()
    assert page.added == []


def test_temp_image_removed_after_graphic_url_and_on_failure(tmp_path):
    """GraphicURL still sees the file; the file is gone after the setter returns or fails."""
    png = tmp_path / "plot.png"
    png.write_bytes(b"png-bytes")
    ctx = MagicMock()
    doc = MagicMock()
    doc.getCurrentController.return_value = None
    sheet = MagicMock()
    page = _DrawPage([])
    sheet.DrawPage = page
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    sheets.getByIndex.return_value = sheet
    doc.getSheets.return_value = sheets
    shape = MagicMock()

    def _set(prop, value):
        if prop == "GraphicURL":
            assert png.exists()

    shape.setPropertyValue.side_effect = _set
    doc.createInstance.return_value = shape

    with (
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value=str(png)),
        patch("uno.systemPathToFileUrl", return_value=png.as_uri()),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, doc=doc)

    assert not png.exists()
    shape.setPropertyValue.assert_any_call("GraphicURL", png.as_uri())

    png.write_bytes(b"png-bytes")
    page.shapes.clear()
    page.added.clear()

    def _set_fail(prop, _value):
        if prop == "GraphicURL":
            assert png.exists()
            raise RuntimeError("graphic load failed")

    shape.setPropertyValue.side_effect = _set_fail
    with (
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value=str(png)),
        patch("uno.systemPathToFileUrl", return_value=png.as_uri()),
        pytest.raises(ImageEgressError),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, doc=doc)

    assert not png.exists()
    # The empty shape must be removed from the draw page on GraphicURL failure
    assert shape not in page.shapes


def test_anchor_failure_logs_warning_and_succeeds(caplog):
    """Failure during Anchor / geometry positioning logs a warning, but returns successfully."""
    ctx = MagicMock()
    doc = MagicMock()
    sheet = MagicMock()
    cell = CalcCellStub(col=0, row=0)
    sheet.getCellByPosition.return_value = cell
    ctrl = MagicMock()
    sel = MagicMock()
    sel.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, StartRow=0)
    ctrl.getSelection.return_value = sel
    doc.getCurrentController.return_value = ctrl
    page = _DrawPage([])
    sheet.DrawPage = page
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    sheets.getByIndex.return_value = sheet
    doc.getSheets.return_value = sheets
    shape = MagicMock()
    doc.createInstance.return_value = shape

    with (
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/plot.svg"),
        patch("plugin.calc.python.image_egress._unlink_temp_image"),
        patch("plugin.calc.calc_utils.get_cell_geometry", side_effect=RuntimeError("geometry error")),
    ):
        import logging
        with caplog.at_level(logging.WARNING):
            insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, doc=doc)

    assert any("could not anchor to cell" in record.message for record in caplog.records)


def test_reused_shape_not_removed_on_failure():
    """A reused shape is not removed from DrawPage when GraphicURL setter fails."""
    cell = CalcCellStub(col=3, row=6)
    plot = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    page = _DrawPage([plot])
    ctx = MagicMock()
    target = MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None

    def _set_fail(prop, _val):
        if prop == "GraphicURL":
            raise RuntimeError("graphic update failed")

    plot.setPropertyValue = _set_fail

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
        pytest.raises(ImageEgressError),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0)

    # Reused shape must still remain on the draw page
    assert plot in page.shapes

