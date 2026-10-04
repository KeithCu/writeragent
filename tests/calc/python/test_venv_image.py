# WriterAgent - tests for run_venv_python_script image handling

from __future__ import annotations

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
    ctx.stop_checker = None

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
    insert.assert_called_once_with(ctx.ctx, _IMAGE_PAYLOAD, doc=target)


def test_calc_image_insert_failure_is_not_success():
    """A missing sheet or draw page must not report the plot as inserted."""
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock(name="target_doc")
    ctx.stop_checker = None

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


def test_calc_image_result_inserts_even_if_stopped():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    target = MagicMock(name="target_doc")
    ctx.doc = target
    ctx.stop_checker = lambda: True

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": _IMAGE_PAYLOAD}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch(
            "plugin.framework.queue_executor.execute_on_main_thread",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ) as main_thread,
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet", return_value=True) as insert,
        patch("plugin.scripting.config_limits.configured_python_max_data_cells", return_value=10000),
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "ok"
    assert out["image_inserted"] is True
    assert out["image_path"] == "/tmp/plot.svg"
    assert main_thread.call_count == 1
    insert.assert_called_once()


def test_insert_image_result_on_sheet_none_doc_is_failure():
    """A missing document is an insert failure, not a silent success."""
    ctx = MagicMock()
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=None),
        pytest.raises(ImageEgressError, match="no Calc document"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD)


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
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=doc),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD)

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
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=doc),
        patch("plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc", return_value=None),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp") as mock_write,
        pytest.raises(ImageEgressError, match="could not locate formula cell"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code=code)

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


def _insert_at_cell(cell: CalcCellStub, page: _DrawPage, *, doc: MagicMock | None = None) -> MagicMock:
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
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target)
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
    # The other cell already uses WriterAgentPlot, so this shape gets the next suffix.
    page.added[0].setPropertyValue.assert_any_call("Name", f"{_PLOT_SHAPE_NAME_PREFIX}_2")


def test_reuse_replaces_named_plot_shape_only():
    """The next plot updates the WriterAgentPlot graphic at that cell."""
    cell = CalcCellStub(col=3, row=6)
    plot = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", cell)
    user_image = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "Picture 1", cell)
    page = _DrawPage([user_image, plot])

    target = _insert_at_cell(cell, page)

    assert plot.props["GraphicURL"] == "file:///tmp/chart.svg"
    assert "GraphicURL" not in user_image.props
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
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=doc),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value=str(png)),
        patch("uno.systemPathToFileUrl", return_value=png.as_uri()),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD)

    assert not png.exists()
    shape.setPropertyValue.assert_any_call("GraphicURL", png.as_uri())

    png.write_bytes(b"png-bytes")

    def _set_fail(prop, _value):
        if prop == "GraphicURL":
            assert png.exists()
            raise RuntimeError("graphic load failed")

    shape.setPropertyValue.side_effect = _set_fail
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=doc),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value=str(png)),
        patch("uno.systemPathToFileUrl", return_value=png.as_uri()),
        pytest.raises(ImageEgressError),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD)

    assert not png.exists()
