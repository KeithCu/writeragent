# WriterAgent - tests for run_venv_python_script image handling

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import pytest

from plugin.calc.python.image_egress import (
    _DRAW_GRAPHIC_SERVICE,
    _IMAGE_ANNOTATION_PREFIX,
    _PLOT_SHAPE_NAME_PREFIX,
    ImageEgressError,
    _intrinsic_aspect,
    _plot_slot_index,
    _plot_slot_name,
    _read_image_bytes,
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
        patch("os.unlink") as mock_unlink,
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is True
    assert "image_path" not in out
    assert "image_paths" not in out
    assert "active sheet" in out["message"]
    assert main_thread.call_count == 1
    insert.assert_called_once_with(ctx.ctx, _IMAGE_PAYLOAD, doc=target, image_index=0, image_path="/tmp/plot.svg", image_count=1)
    mock_unlink.assert_called_once_with("/tmp/plot.svg")


def test_stdout_and_result_survives_image_result():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    target = MagicMock(name="target_doc")
    ctx.doc = target

    payload = {"status": "ok", "result": [_IMAGE_PAYLOAD, 42], "stdout": "printed output\n"}
    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value=payload),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch(
            "plugin.framework.queue_executor.execute_on_main_thread",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet"),
        patch("plugin.scripting.config_limits.configured_python_max_data_cells", return_value=10000),
        patch("os.unlink"),
    ):
        out = tool.execute(ctx, code="print('printed output'); plt.plot([1]); result = 42")

    assert out["status"] == "ok"
    assert out["stdout"] == "printed output\n"
    assert out["result"] == ["<image>", 42]


def test_write_image_payload_to_temp_not_called_from_egress_when_image_path_supplied():
    ctx = MagicMock()
    doc = MagicMock()
    sheet = MagicMock()
    draw_page = MagicMock()
    sheet.DrawPage = draw_page
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    sheets.getByIndex.return_value = sheet
    doc.getSheets.return_value = sheets
    doc.getCurrentController.return_value = None
    shape = MagicMock()
    doc.createInstance.return_value = shape

    with (
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp") as mock_write,
        patch("plugin.calc.python.image_egress._unlink_temp_image") as mock_unlink,
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/supplied.svg"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, doc=doc, image_path="/tmp/supplied.svg")

    mock_write.assert_not_called()
    shape.setPropertyValue.assert_called_with("GraphicURL", "file:///tmp/supplied.svg")
    # Egress did not create the temp file, so it must not unlink it
    mock_unlink.assert_not_called()



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


def test_page_anchored_shape_matches_no_cell():
    """A page-anchored shape's Anchor is the sheet; its range starts at 0,0 but it is not A1."""

    class _SheetAnchor:
        def supportsService(self, name):
            return name == "com.sun.star.sheet.Spreadsheet"

        def getRangeAddress(self):
            return MagicMock(Sheet=None, StartColumn=0, StartRow=0)

    shape = MagicMock()
    shape.getPropertyValue.return_value = _SheetAnchor()
    assert not _shape_anchor_matches_cell(shape, CalcCellStub(col=0, row=0))


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


def test_image_count_removes_higher_slot():
    """image_index=0, image_count=1 removes WriterAgentPlot_2 at that cell and leaves user image and other plot."""
    cell = CalcCellStub(col=3, row=6)
    other = CalcCellStub(col=0, row=0)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", cell)
    user_img = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "UserPhoto", cell)
    other_plot = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", other)
    page = _DrawPage([slot0, slot1, user_img, other_plot])

    ctx = MagicMock()
    target = MagicMock(name="target")
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
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0, image_count=1)

    assert slot1 not in page.shapes
    assert slot0 in page.shapes
    assert user_img in page.shapes
    assert other_plot in page.shapes


def test_failed_insert_does_not_sweep_extra_slots():
    """If insert fails, stale slot shapes are not swept."""
    cell = CalcCellStub(col=3, row=6)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", cell)
    page = _DrawPage([slot0, slot1])

    ctx = MagicMock()
    target = MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None

    def _set_fail(prop, _val):
        if prop == "GraphicURL":
            raise RuntimeError("graphic update failed")

    slot0.setPropertyValue = _set_fail

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
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0, image_count=1)

    assert slot1 in page.shapes


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


def test_svg_aspect_not_stretched():
    from com.sun.star.awt import Size

    svg_data = b'<svg xmlns="http://www.w3.org/2000/svg" width="8in" height="4in"></svg>'
    svg_payload = {
        "__wa_payload__": PAYLOAD_IMAGE,
        "format": "svg",
        "data": svg_data,
    }
    cell = CalcCellStub(col=3, row=6)
    page = _DrawPage([])
    ctx = MagicMock()
    target = MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None
    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    target.createInstance.return_value = shape

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
        insert_image_result_on_sheet(ctx, svg_payload, code="plt.show()", doc=target, image_index=0)

    assert shape.props.get("Size") == Size(10000, 5000)


def test_png_aspect():
    import struct
    from com.sun.star.awt import Size

    # 8-byte signature + 4 bytes length + 4 bytes IHDR + 8 bytes (w=100, h=50)
    png_data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 100, 50) + b"\x08\x06\x00\x00\x00\x00\x00\x00\x00"
    png_payload = {
        "__wa_payload__": PAYLOAD_IMAGE,
        "format": "png",
        "data": png_data,
    }
    cell = CalcCellStub(col=3, row=6)
    page = _DrawPage([])
    ctx = MagicMock()
    target = MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None
    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    target.createInstance.return_value = shape

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.png"),
        patch("plugin.calc.calc_utils.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        insert_image_result_on_sheet(ctx, png_payload, code="plt.show()", doc=target, image_index=0)

    assert shape.props.get("Size") == Size(10000, 5000)


def test_read_image_bytes_reads_only_header():
    m = mock_open(read_data=b"x" * 20000)
    with (
        patch("builtins.open", m),
        patch("plugin.calc.python.image_egress.os.path.isfile", return_value=True),
    ):
        _read_image_bytes(None, "/tmp/x.png")
    m().read.assert_called_once_with(8192)


def test_intrinsic_aspect_handles_mixed_svg_units():
    assert _intrinsic_aspect(b'<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="192px"></svg>') == (50800, 50800)


def test_anchor_failure_removes_new_shape_and_raises():
    """setPropertyValue('Anchor') raises, draw_page.remove was called, ImageEgressError propagates."""
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

    def _set_prop(prop, val):
        if prop == "Anchor":
            raise RuntimeError("anchor error")

    shape.setPropertyValue.side_effect = _set_prop
    doc.createInstance.return_value = shape

    with (
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/plot.svg"),
        patch("plugin.calc.python.image_egress._unlink_temp_image"),
        patch("plugin.calc.calc_utils.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
        pytest.raises(ImageEgressError, match="could not anchor plot to the formula cell"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, doc=doc)

    assert shape not in page.shapes


def test_off_main_failure_annotates_and_does_not_raise():
    """When off-main with notify_cell=True, an ImageEgressError sets an annotation and does not raise."""
    ctx = MagicMock()
    doc = MagicMock()
    sheet = MagicMock()
    cell = CalcCellStub(col=0, row=0)
    ann = MagicMock()
    ann_text = []
    ann.setString.side_effect = lambda t: ann_text.append(t)
    ann.getString.return_value = ""
    cell.getAnnotation = lambda: ann

    annotations = MagicMock()
    inserted_annotations = []
    annotations.insertNew.side_effect = lambda addr, text: inserted_annotations.append(text)
    sheet.getAnnotations.return_value = annotations

    posted_calls = []

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("test egress failure"),
        ),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

    assert any("WriterAgent image: test egress failure" in t for t in inserted_annotations + ann_text)


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


def test_off_main_failure_does_not_replace_user_annotation():
    """A user comment (including whitespace) is never replaced by an egress failure."""
    ctx = MagicMock()
    doc = MagicMock()
    sheet = MagicMock()
    cell = CalcCellStub(col=0, row=0)
    ann = MagicMock()
    ann.getString.return_value = "keep me"
    cell.getAnnotation = lambda: ann

    annotations = MagicMock()
    sheet.getAnnotations.return_value = annotations

    posted_calls = []

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("test egress failure"),
        ),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

        ann.setString.assert_not_called()
        annotations.insertNew.assert_not_called()

        # Second case with whitespace comment
        ann.getString.return_value = " "
        posted_calls.clear()
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

        ann.setString.assert_not_called()
        annotations.insertNew.assert_not_called()


def test_off_main_failure_updates_existing_writeragent_annotation():
    """An existing WriterAgent image: note is updated in place via setString without calling insertNew."""
    ctx = MagicMock()
    doc = MagicMock()
    sheet = MagicMock()
    cell = CalcCellStub(col=0, row=0)
    ann = MagicMock()
    ann.getString.return_value = f"{_IMAGE_ANNOTATION_PREFIX}old"
    cell.getAnnotation = lambda: ann

    annotations = MagicMock()
    sheet.getAnnotations.return_value = annotations

    posted_calls = []

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("new error text"),
        ),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

    ann.setString.assert_called_once()
    called_text = ann.setString.call_args[0][0]
    assert called_text.startswith(_IMAGE_ANNOTATION_PREFIX)
    assert "new error text" in called_text
    annotations.insertNew.assert_not_called()


def test_off_main_annotation_requires_cell_address():
    """When getCellAddress fails, insertNew is not called."""
    ctx = MagicMock()
    doc = MagicMock()
    sheet = MagicMock()
    cell = MagicMock()
    ann = MagicMock()
    ann.getString.return_value = ""
    cell.getAnnotation = lambda: ann
    cell.getCellAddress.side_effect = RuntimeError("no address")

    annotations = MagicMock()
    sheet.getAnnotations.return_value = annotations

    posted_calls = []

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("test egress failure"),
        ),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

    annotations.insertNew.assert_not_called()


def test_off_main_success_clears_only_writeragent_annotation():
    """Success clears only annotations starting with WriterAgent image:, leaving user notes intact."""
    ctx = MagicMock()
    doc = MagicMock()
    sheet = MagicMock()
    cell = CalcCellStub(col=2, row=5)
    ann = MagicMock()
    ann.getString.return_value = "keep me"
    cell.getAnnotation = lambda: ann

    annotations = MagicMock()
    sheet.getAnnotations.return_value = annotations

    posted_calls = []

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (5, 2)),
        ),
        patch("plugin.calc.python.image_egress._insert_image_result_on_sheet_impl"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

        annotations.removeByIndex.assert_not_called()
        ann.setString.assert_not_called()

        # Other case: note starts with prefix, annotations list has matching item
        ann.getString.return_value = f"{_IMAGE_ANNOTATION_PREFIX}old error"
        item = MagicMock()
        item.getPosition.return_value = SimpleNamespace(Column=2, Row=5)
        annotations.getCount.return_value = 1
        annotations.getByIndex.return_value = item

        posted_calls.clear()
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

        annotations.removeByIndex.assert_called_once_with(0)


def test_main_thread_failure_raises_without_annotation():
    """Main-thread insert failure raises ImageEgressError without setting cell annotations."""
    ctx = MagicMock()
    doc = MagicMock()
    sheet = MagicMock()
    cell = CalcCellStub(col=0, row=0)
    ann = MagicMock()
    cell.getAnnotation = lambda: ann
    annotations = MagicMock()
    sheet.getAnnotations.return_value = annotations

    with (
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("boom"),
        ),
        pytest.raises(ImageEgressError, match="boom"),
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)

    ann.setString.assert_not_called()
    annotations.insertNew.assert_not_called()


def test_plot_slot_index_round_trip_and_rejects_int_junk():
    assert _plot_slot_name(0) == "WriterAgentPlot"
    assert _plot_slot_index("WriterAgentPlot") == 0
    assert _plot_slot_name(1) == "WriterAgentPlot_2"
    assert _plot_slot_index("WriterAgentPlot_2") == 1
    assert _plot_slot_index("WriterAgentPlot_10") == 9

    assert _plot_slot_index("WriterAgentPlot_1") is None
    assert _plot_slot_index("WriterAgentPlot_02") is None
    assert _plot_slot_index("WriterAgentPlot_+2") is None
    assert _plot_slot_index("WriterAgentPlot_1_0") is None
    assert _plot_slot_index("WriterAgentPlot_ 2") is None
    assert _plot_slot_index("WriterAgentPlot_2 ") is None


def test_image_count_keeps_non_decimal_names():
    """Only valid decimal plot slot names >= image_count are dropped; non-decimal names are preserved."""
    cell = CalcCellStub(col=3, row=6)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot2 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", cell)
    slot10 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_10", cell)

    junk_1_0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_1_0", cell)
    junk_plus = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_+2", cell)
    junk_02 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_02", cell)
    junk_1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_1", cell)
    user_photo = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "UserPhoto", cell)

    page = _DrawPage([slot0, slot2, slot10, junk_1_0, junk_plus, junk_02, junk_1, user_photo])

    ctx = MagicMock()
    target = MagicMock(name="target")
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
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0, image_count=1)

    assert slot2 not in page.shapes
    assert slot10 not in page.shapes
    assert slot0 in page.shapes
    assert junk_1_0 in page.shapes
    assert junk_plus in page.shapes
    assert junk_02 in page.shapes
    assert junk_1 in page.shapes
    assert user_photo in page.shapes


def test_temp_images_unlinked_when_a_later_write_fails():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    target = MagicMock(name="target_doc")
    ctx.doc = target

    img1 = dict(_IMAGE_PAYLOAD)
    img2 = dict(_IMAGE_PAYLOAD)

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": [img1, img2]}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", side_effect=["/tmp/plot0.svg", OSError("second write failed")]),
        patch(
            "plugin.framework.queue_executor.execute_on_main_thread",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet") as mock_insert,
        patch("plugin.scripting.config_limits.configured_python_max_data_cells", return_value=10000),
        patch("os.unlink") as mock_unlink,
        pytest.raises(OSError, match="second write failed"),
    ):
        tool.execute(ctx, code="plt.plot([1]); plt.plot([2])")

    mock_unlink.assert_called_once_with("/tmp/plot0.svg")
    mock_insert.assert_not_called()


def test_partial_insert_failure_unlinks_only_embedded_temps():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    target = MagicMock(name="target_doc")
    ctx.doc = target

    img1 = dict(_IMAGE_PAYLOAD)
    img2 = dict(_IMAGE_PAYLOAD)

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": [img1, img2]}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", side_effect=["/tmp/plot0.svg", "/tmp/plot1.svg"]),
        patch(
            "plugin.framework.queue_executor.execute_on_main_thread",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch(
            "plugin.calc.python.image_egress.insert_image_result_on_sheet",
            side_effect=[None, ImageEgressError("second insert failed")],
        ),
        patch("plugin.scripting.config_limits.configured_python_max_data_cells", return_value=10000),
        patch("os.unlink") as mock_unlink,
    ):
        out = tool.execute(ctx, code="plt.plot([1]); plt.plot([2])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is True
    assert "1 of 2" in out["message"]
    mock_unlink.assert_called_once_with("/tmp/plot0.svg")
    assert out["image_paths"] == ["/tmp/plot1.svg"]
    assert out["image_path"] == "/tmp/plot1.svg"


