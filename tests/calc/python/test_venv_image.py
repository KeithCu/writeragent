# WriterAgent - tests for run_venv_python_script image handling

from __future__ import annotations

import hashlib
import struct
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import uno  # noqa: F401
from com.sun.star.awt import Point, Size

from plugin.calc.python.image_egress import (
    _DRAW_GRAPHIC_SERVICE,
    _IMAGE_ANNOTATION_PREFIX,
    _PLOT_SHAPE_NAME_PREFIX,
    _PLOT_TAG_ATTR,
    ImageEgressError,
    _aspect_ratio_changed,
    _format_plot_tag,
    _intrinsic_aspect,
    _parse_plot_tag,
    _plot_slot_index,
    _plot_slot_name,
    _read_image_bytes,
    _shape_anchor_matches_cell,
    insert_image_result_on_sheet,
)
from plugin.tests.testing_utils import CalcCellStub
from plugin.calc.python.venv import RunVenvPythonScript
from plugin.scripting.payload_codec import PAYLOAD_IMAGE


def _make_png_payload(w: int, h: int) -> dict[str, Any]:
    header = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00\x00\x00\x00\x00"
    )
    return {
        "__wa_payload__": PAYLOAD_IMAGE,
        "format": "png",
        "data": header,
    }

_IMAGE_PAYLOAD = {
    "__wa_payload__": PAYLOAD_IMAGE,
    "format": "svg",
    "data": b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
}


def _plot_tag(data: bytes, col: int, row: int) -> str:
    """Owner and digest tag the egress code writes for *data* at (col, row)."""
    return _format_plot_tag(col, row, hashlib.sha256(data).hexdigest())


class _AttrContainer:
    """Name container standing in for a shape's UserDefinedAttributes."""

    def __init__(self) -> None:
        self.items: dict[str, Any] = {}

    def hasByName(self, name: str) -> bool:
        return name in self.items

    def getByName(self, name: str) -> Any:
        return self.items[name]

    def insertByName(self, name: str, value: Any) -> None:
        self.items[name] = value

    def replaceByName(self, name: str, value: Any) -> None:
        self.items[name] = value


def _set_tag(shape: Any, tag: str | None) -> None:
    """Set (or with None, clear) the wa-plot tag on a stub shape."""
    attrs = _AttrContainer()
    if tag is not None:
        attrs.items[_PLOT_TAG_ATTR] = SimpleNamespace(Value=tag)
    shape.props["UserDefinedAttributes"] = attrs


def _get_tag(shape: Any) -> str | None:
    item = shape.props["UserDefinedAttributes"].items.get(_PLOT_TAG_ATTR)
    return None if item is None else item.Value


def _graphic(width: int, height: int) -> Any:
    """Stand-in for a shape's Graphic whose logical size gives its aspect."""
    graphic = MagicMock()
    graphic.getPropertyValue.side_effect = lambda name: Size(width, height) if name == "Size100thMM" else None
    return graphic


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
            "plugin.framework.queue_executor.main.call",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ) as main_thread,
        patch("plugin.calc.python.venv.insert_image_result_on_sheet") as insert,
        patch("plugin.calc.calc_addin_data.configured_python_max_data_cells", return_value=10000),
        patch("os.unlink") as mock_unlink,
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is True
    assert "image_path" not in out
    assert "image_paths" not in out
    assert "active sheet" in out["message"]
    assert main_thread.call_count == 1
    insert.assert_called_once_with(_IMAGE_PAYLOAD, doc=target, image_index=0, image_path="/tmp/plot.svg", image_count=1)
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
            "plugin.framework.queue_executor.main.call",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch("plugin.calc.python.venv.insert_image_result_on_sheet"),
        patch("plugin.calc.calc_addin_data.configured_python_max_data_cells", return_value=10000),
        patch("os.unlink"),
    ):
        out = tool.execute(ctx, code="print('printed output'); plt.plot([1]); result = 42")

    assert out["status"] == "ok"
    assert out["stdout"] == "printed output\n"
    assert out["result"] == ["<image>", 42]


def test_write_image_payload_to_temp_not_called_from_egress_when_image_path_supplied():
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
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, doc=doc, image_path="/tmp/supplied.svg")

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
            "plugin.framework.queue_executor.main.call",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch(
            "plugin.calc.python.venv.insert_image_result_on_sheet",
            side_effect=ImageEgressError("target sheet has no DrawPage; image was not inserted"),
        ),
        patch("plugin.calc.calc_addin_data.configured_python_max_data_cells", return_value=10000),
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "error"
    assert out["image_inserted"] is False
    assert "not inserted" in out["message"]
    assert "active sheet" not in out["message"]


def test_all_plot_inserts_fail_reports_first_error_and_keeps_every_temp():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock(name="target_doc")
    two_images = {
        "__writeragent_type__": "image_list",
        "items": [_IMAGE_PAYLOAD, _IMAGE_PAYLOAD],
    }

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": two_images}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", side_effect=["/tmp/p0.svg", "/tmp/p1.svg"]),
        patch(
            "plugin.framework.queue_executor.main.call",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch(
            "plugin.calc.python.venv.insert_image_result_on_sheet",
            side_effect=[ImageEgressError("e1"), ImageEgressError("e2")],
        ),
        patch("plugin.calc.python.venv.os.unlink") as mock_unlink,
        patch("plugin.calc.calc_addin_data.configured_python_max_data_cells", return_value=10000),
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "error"
    assert out["message"] == "Plot was not inserted: e1"
    assert out["plot_error"] == "e1; e2"
    assert out["image_paths"] == ["/tmp/p0.svg", "/tmp/p1.svg"]
    assert out["image_path"] == out["image_paths"]
    mock_unlink.assert_not_called()


def test_writer_image_result_returns_path_only():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "writer"
    ctx.ctx = MagicMock()

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": _IMAGE_PAYLOAD}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch("plugin.calc.python.venv.insert_image_result_on_sheet"),
        patch("plugin.calc.calc_addin_data.configured_python_max_data_cells", return_value=10000),
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is None
    assert out["image_path"] == "/tmp/plot.svg"


def test_insert_image_result_on_sheet_none_doc_is_failure():
    """A missing document is an insert failure, not a silent success."""
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx") as front_window,
        pytest.raises(ImageEgressError, match="no Calc document"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD)
    # The front window is never used as a fallback.
    front_window.assert_not_called()


def test_insert_image_result_on_sheet_active_sheet_fallback():
    """Fallback to getSheets().getByIndex(0) when getCurrentController is None."""
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
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, doc=doc)

    assert draw_page.add.call_count == 1
    shape.setPropertyValue.assert_called_with("GraphicURL", "file:///tmp/chart.svg")


def test_insert_image_result_on_sheet_background_thread_marshaling():
    """When called off main thread, insert_image_result_on_sheet posts asynchronously to main thread."""
    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.post") as post_main,
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD)

    assert post_main.call_count == 1


def test_insert_image_result_on_sheet_uses_passed_doc_not_front_window():
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
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ) as locate,
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        target.createInstance.return_value = MagicMock()
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=target)
    locate.assert_called()
    assert locate.call_args[0][0] is target


def test_insert_image_result_on_sheet_aborts_when_formula_location_fails_for_code():
    """When code is supplied and formula cell location fails, image egress aborts without inserting on active sheet."""
    doc = MagicMock()
    ctrl = MagicMock()
    active_sheet = MagicMock()
    draw_page = MagicMock()
    active_sheet.DrawPage = draw_page
    ctrl.getActiveSheet.return_value = active_sheet
    doc.getCurrentController.return_value = ctrl

    code = "import matplotlib.pyplot as plt; plt.plot([1, 2, 3])"

    with (
        patch("plugin.calc.python.image_egress.locate_formula_cell_in_doc", return_value=None),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp") as mock_write,
        pytest.raises(ImageEgressError, match="could not locate formula cell"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code=code, doc=doc)

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
        self.props: dict[str, object] = {
            "Name": name,
            "Anchor": anchor,
            "ResizeWithCell": False,
            "UserDefinedAttributes": _AttrContainer(),
        }

    def supportsService(self, service: str) -> bool:
        return service == self.service

    def getPropertyValue(self, key: str):
        return self.props.get(key)

    def setPropertyValue(self, key: str, value: object) -> None:
        self.props[key] = value

    def setSize(self, size: object) -> None:
        self.props["Size"] = size

    def getSize(self):
        return self.props.get("Size")

    def setPosition(self, pos: object) -> None:
        self.props["Position"] = pos

    def getPosition(self):
        return self.props.get("Position")


class _SheetStub:
    """Sheet stub with drawing page, optional table layout, and row/column visibility."""

    def __init__(
        self,
        page: _DrawPage,
        *,
        table_layout: int = 0,
        rows_visible: bool = True,
        cols_visible: bool = True,
    ):
        self.DrawPage = page
        self.TableLayout = table_layout
        self._rows_visible = rows_visible
        self._cols_visible = cols_visible

    def getPropertyValue(self, name: str):
        if name == "TableLayout":
            return self.TableLayout
        return None

    def getRows(self):
        row = MagicMock()
        row.IsVisible = self._rows_visible
        rows = MagicMock()
        rows.getByIndex.return_value = row
        return rows

    def getColumns(self):
        col = MagicMock()
        col.IsVisible = self._cols_visible
        cols = MagicMock()
        cols.getByIndex.return_value = col
        return cols


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
    target = doc or MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        target.createInstance.return_value = MagicMock()
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=image_index)
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

    sheet = MagicMock()
    sheet.DrawPage = page

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", side_effect=["/tmp/chart0.svg", "/tmp/chart1.svg"]),
        patch("uno.systemPathToFileUrl", side_effect=["file:///tmp/chart0.svg", "file:///tmp/chart1.svg"]),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(MagicMock(X=1000, Y=2000), MagicMock(Width=5000, Height=4000))),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0)
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=1)

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

    target = MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        target.createInstance.return_value = MagicMock()
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0, image_count=1)

    assert slot1 not in page.shapes
    assert slot0 in page.shapes
    assert user_img in page.shapes
    assert other_plot in page.shapes


def test_failed_insert_sweeps_extra_slots_while_preserving_slot0():
    """If insert fails on slot 0, extra slot shapes are swept while slot 0 is preserved."""
    cell = CalcCellStub(col=3, row=6)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", cell)
    page = _DrawPage([slot0, slot1])

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
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
    ):
        # A failed reload is only logged, so the sweep and later slots still run.
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0, image_count=1)

    assert slot0 in page.shapes
    assert slot1 not in page.shapes


def test_temp_image_removed_after_graphic_url_and_on_failure(tmp_path):
    """GraphicURL still sees the file; the file is gone after the setter returns or fails."""
    png = tmp_path / "plot.png"
    png.write_bytes(b"png-bytes")
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
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, doc=doc)

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
        pytest.raises(ImageEgressError, match="could not load image into graphic shape"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, doc=doc)

    assert not png.exists()
    # Without a target cell, a failed graphic load removes the temporary shape
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
    target = MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None
    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    target.createInstance.return_value = shape

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        insert_image_result_on_sheet(svg_payload, code="plt.show()", doc=target, image_index=0)

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
    target = MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None
    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    target.createInstance.return_value = shape

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.png"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        insert_image_result_on_sheet(png_payload, code="plt.show()", doc=target, image_index=0)

    assert shape.props.get("Size") == Size(10000, 5000)


def test_read_image_bytes_reads_full_file(tmp_path):
    data = bytes(range(256)) * 80  # > 8 KB
    img_path = tmp_path / "x.png"
    img_path.write_bytes(data)
    out = _read_image_bytes(None, str(img_path))
    assert out is not None
    assert len(out) == len(data)
    assert hashlib.sha256(out).digest() == hashlib.sha256(data).digest()


def test_intrinsic_aspect_handles_mixed_svg_units():
    assert _intrinsic_aspect(b'<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="192px"></svg>') == (50800, 50800)


def test_anchor_failure_removes_new_shape_and_raises():
    """setPropertyValue('Anchor') raises, draw_page.remove was called, ImageEgressError propagates."""
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
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
        pytest.raises(ImageEgressError, match="could not anchor plot to the formula cell"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, doc=doc)

    assert shape not in page.shapes


def test_off_main_failure_annotates_and_does_not_raise():
    """When off-main with notify_cell=True, an ImageEgressError sets an annotation and does not raise."""
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
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("test egress failure"),
        ),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

    assert any("WriterAgent image: test egress failure" in t for t in inserted_annotations + ann_text)


def test_reused_shape_not_removed_on_failure():
    """A reused shape whose GraphicURL fails stays on the page with its rectangle, without raising."""
    cell = CalcCellStub(col=3, row=6)
    plot = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    plot.setPosition(Point(100, 200))
    plot.setSize(Size(3000, 2000))
    page = _DrawPage([plot])
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
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
    ):
        # Revert target: log-and-return on GraphicURL failure in _refresh_existing_plot
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0)

    # Reused shape remains on the draw page, position and size stay
    assert plot in page.shapes
    assert plot.getPosition() == Point(100, 200)
    assert plot.getSize() == Size(3000, 2000)


def test_off_main_failure_does_not_replace_user_annotation():
    """A user comment (including whitespace) is never replaced by an egress failure."""
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
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("test egress failure"),
        ),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

        ann.setString.assert_not_called()
        annotations.insertNew.assert_not_called()

        # Second case with whitespace comment
        ann.getString.return_value = " "
        posted_calls.clear()
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

        ann.setString.assert_not_called()
        annotations.insertNew.assert_not_called()


def test_off_main_failure_updates_existing_writeragent_annotation():
    """An existing WriterAgent image: note is updated in place via setString without calling insertNew."""
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
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("new error text"),
        ),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

    ann.setString.assert_called_once()
    called_text = ann.setString.call_args[0][0]
    assert called_text.startswith(_IMAGE_ANNOTATION_PREFIX)
    assert "new error text" in called_text
    annotations.insertNew.assert_not_called()


def test_off_main_annotation_requires_cell_address():
    """When getCellAddress fails, insertNew is not called."""
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
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("test egress failure"),
        ),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

    annotations.insertNew.assert_not_called()


def test_off_main_success_clears_only_writeragent_annotation():
    """Success clears only annotations starting with WriterAgent image:, leaving user notes intact."""
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
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: posted_calls.append(fn)),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (5, 2)),
        ),
        patch("plugin.calc.python.image_egress._insert_image_result_on_sheet_impl"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
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
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)
        assert len(posted_calls) == 1
        posted_calls[0]()

        annotations.removeByIndex.assert_called_once_with(0)


def test_main_thread_failure_raises_without_annotation():
    """Main-thread insert failure raises ImageEgressError without setting cell annotations."""
    doc = MagicMock()
    sheet = MagicMock()
    cell = CalcCellStub(col=0, row=0)
    ann = MagicMock()
    cell.getAnnotation = lambda: ann
    annotations = MagicMock()
    sheet.getAnnotations.return_value = annotations

    with (
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch(
            "plugin.calc.python.image_egress._insert_image_result_on_sheet_impl",
            side_effect=ImageEgressError("boom"),
        ),
        pytest.raises(ImageEgressError, match="boom"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, notify_cell=True)

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

    target = MagicMock(name="target")
    sheet = MagicMock()
    sheet.DrawPage = page
    target.getCurrentController.return_value = None
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/chart.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/chart.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(MagicMock(), MagicMock(Width=5000, Height=4000))),
    ):
        target.createInstance.return_value = MagicMock()
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=target, image_index=0, image_count=1)

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
            "plugin.framework.queue_executor.main.call",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch("plugin.calc.python.venv.insert_image_result_on_sheet") as mock_insert,
        patch("plugin.calc.calc_addin_data.configured_python_max_data_cells", return_value=10000),
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
            "plugin.framework.queue_executor.main.call",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch(
            "plugin.calc.python.venv.insert_image_result_on_sheet",
            side_effect=[None, ImageEgressError("second insert failed")],
        ),
        patch("plugin.calc.calc_addin_data.configured_python_max_data_cells", return_value=10000),
        patch("os.unlink") as mock_unlink,
    ):
        out = tool.execute(ctx, code="plt.plot([1]); plt.plot([2])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is True
    assert "1 of 2" in out["message"]
    mock_unlink.assert_called_once_with("/tmp/plot0.svg")
    assert out["image_paths"] == ["/tmp/plot1.svg"]
    assert out["image_path"] == "/tmp/plot1.svg"


def test_middle_insert_failure_continues_later_plots():
    """A failed middle slot still inserts later plots; only the failed path is returned."""
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.ctx = MagicMock()
    target = MagicMock(name="target_doc")
    ctx.doc = target

    img1 = dict(_IMAGE_PAYLOAD)
    img2 = dict(_IMAGE_PAYLOAD)
    img3 = dict(_IMAGE_PAYLOAD)

    with (
        patch(
            "plugin.calc.python.venv.run_code_in_user_venv",
            return_value={"status": "ok", "result": [img1, img2, img3]},
        ),
        patch(
            "plugin.calc.python.venv.write_image_payload_to_temp",
            side_effect=["/tmp/plot0.svg", "/tmp/plot1.svg", "/tmp/plot2.svg"],
        ),
        patch(
            "plugin.framework.queue_executor.main.call",
            side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        ),
        patch(
            "plugin.calc.python.venv.insert_image_result_on_sheet",
            side_effect=[None, ImageEgressError("middle insert failed"), None],
        ) as mock_insert,
        patch("plugin.calc.calc_addin_data.configured_python_max_data_cells", return_value=10000),
        patch("os.unlink") as mock_unlink,
    ):
        out = tool.execute(ctx, code="plt.plot([1]); plt.plot([2]); plt.plot([3])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is True
    assert "2 of 3" in out["message"]
    assert out["plot_error"] == "middle insert failed"
    assert mock_insert.call_count == 3
    assert mock_unlink.call_count == 2
    mock_unlink.assert_any_call("/tmp/plot0.svg")
    mock_unlink.assert_any_call("/tmp/plot2.svg")
    assert out["image_paths"] == ["/tmp/plot1.svg"]
    assert out["image_path"] == "/tmp/plot1.svg"


def test_mixed_aspect_slots_chain_edges():
    """Slot 0 aspect 100x50 and slot 1 aspect 50x100 chain flush at slot 0's right edge."""
    cell = CalcCellStub(col=1, row=2)
    page = _DrawPage([])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    created_shapes: list[_AnchoredShape] = []

    def _create_shape(service):
        shape = _AnchoredShape(service, "", cell)
        created_shapes.append(shape)
        return shape

    doc.createInstance.side_effect = _create_shape

    cell_pos = Point(1000, 2000)
    cell_size = Size(3000, 1000)
    payload0 = _make_png_payload(100, 50)
    payload1 = _make_png_payload(50, 100)

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", side_effect=["/tmp/p0.png", "/tmp/p1.png"]),
        patch("uno.systemPathToFileUrl", side_effect=["file:///tmp/p0.png", "file:///tmp/p1.png"]),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(cell_pos, cell_size)),
    ):
        insert_image_result_on_sheet(payload0, code="plt.show()", doc=doc, image_index=0)
        insert_image_result_on_sheet(payload1, code="plt.show()", doc=doc, image_index=1)

    assert len(created_shapes) == 2
    slot0, slot1 = created_shapes
    assert slot0.getSize() == Size(10000, 5000)
    assert slot0.getPosition() == Point(1000, 2000)
    assert slot1.getSize() == Size(5000, 10000)
    # Revert target: _slot_cursor chaining. Without chaining, slot 1 would be placed at 1000 + 1 * 5000 = 6000
    assert slot1.getPosition() == Point(11000, 2000)


def test_large_merge_slot0_fills_and_slot1_chains_at_aspect():
    """Slot 0 fills large merge; slot 1 preserves aspect size and starts at merged block's edge."""
    cell = CalcCellStub(col=1, row=2)
    cell.IsMerged = True
    page = _DrawPage([])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    created_shapes: list[_AnchoredShape] = []

    def _create_shape(service):
        shape = _AnchoredShape(service, "", cell)
        created_shapes.append(shape)
        return shape

    doc.createInstance.side_effect = _create_shape

    cell_pos = Point(1000, 2000)
    cell_size = Size(8000, 5000)
    payload0 = _make_png_payload(100, 50)
    payload1 = _make_png_payload(100, 50)

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", side_effect=["/tmp/m0.png", "/tmp/m1.png"]),
        patch("uno.systemPathToFileUrl", side_effect=["file:///tmp/m0.png", "file:///tmp/m1.png"]),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(cell_pos, cell_size)),
    ):
        insert_image_result_on_sheet(payload0, code="plt.show()", doc=doc, image_index=0)
        insert_image_result_on_sheet(payload1, code="plt.show()", doc=doc, image_index=1)

    assert len(created_shapes) == 2
    slot0, slot1 = created_shapes
    assert slot0.getPropertyValue("ResizeWithCell") is True
    assert slot0.getSize() == Size(8000, 5000)
    # Revert target: the fill branch requiring image_index == 0. Without it, slot 1 also fills.
    assert slot1.getPropertyValue("ResizeWithCell") is False
    assert slot1.getSize() == Size(10000, 5000)
    assert slot1.getPosition() == Point(9000, 2000)


def test_rtl_sheet_slots_layout_right_to_left():
    """In RTL sheets (TableLayout=1), slots chain from right to left."""
    cell = CalcCellStub(col=1, row=2)
    page = _DrawPage([])
    sheet = _SheetStub(page, table_layout=1)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    created_shapes: list[_AnchoredShape] = []

    def _create_shape(service):
        shape = _AnchoredShape(service, "", cell)
        created_shapes.append(shape)
        return shape

    doc.createInstance.side_effect = _create_shape

    cell_pos = Point(-3000, 100)
    cell_size = Size(3000, 1000)
    payload0 = _make_png_payload(100, 60)
    payload1 = _make_png_payload(50, 100)

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", side_effect=["/tmp/r0.png", "/tmp/r1.png"]),
        patch("uno.systemPathToFileUrl", side_effect=["file:///tmp/r0.png", "file:///tmp/r1.png"]),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(cell_pos, cell_size)),
    ):
        insert_image_result_on_sheet(payload0, code="plt.show()", doc=doc, image_index=0)
        insert_image_result_on_sheet(payload1, code="plt.show()", doc=doc, image_index=1)

    assert len(created_shapes) == 2
    slot0, slot1 = created_shapes
    # Revert target: RTL place-x calculation (cursor_x - target_width)
    assert slot0.getPosition() == Point(-10000, 100)
    assert slot1.getPosition() == Point(-15000, 100)


def test_existing_slot_matching_byte_tag_skips_graphic_and_layout():
    """An existing slot whose tag matches the byte digest skips reload and geometry."""
    cell = CalcCellStub(col=3, row=6)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    payload = _make_png_payload(100, 50)
    _set_tag(slot0, _plot_tag(payload["data"], col=3, row=6))
    slot0.setPosition(Point(500, 600))
    slot0.setSize(Size(8000, 4000))
    page = _DrawPage([slot0])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/match.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/match.png"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(1000, 2000), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(payload, code="plt.show()", doc=doc, image_index=0)

    # Revert target: early match return on matching byte tag
    assert slot0.getPropertyValue("GraphicURL") is None
    assert slot0.getPosition() == Point(500, 600)
    assert slot0.getSize() == Size(8000, 4000)


def test_existing_non_fill_slot_same_aspect_keeps_rectangle():
    """An existing slot with changed bytes but same aspect ratio updates graphic and keeps rectangle."""
    cell = CalcCellStub(col=3, row=6)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot0.setPosition(Point(500, 600))
    slot0.setSize(Size(8000, 4000))
    slot0.setPropertyValue("Graphic", _graphic(10000, 5000))
    page = _DrawPage([slot0])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    payload = _make_png_payload(200, 100)
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/same_aspect.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/same_aspect.png"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(1000, 2000), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(payload, code="plt.show()", doc=doc, image_index=0)

    assert slot0.getPropertyValue("GraphicURL") == "file:///tmp/same_aspect.png"
    # Revert target: update path not snapping existing slots back to cell origin
    assert slot0.getPosition() == Point(500, 600)
    assert slot0.getSize() == Size(8000, 4000)


def test_existing_non_fill_slot_aspect_ratio_change_and_threshold():
    """Existing slot updates size when aspect changes by >1%; preserves size when <=1% or ResizeWithCell."""
    # Direct helper checks
    assert not _aspect_ratio_changed(200, 100, 202, 100)
    assert _aspect_ratio_changed(200, 100, 203, 100)

    cell = CalcCellStub(col=3, row=6)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot0.setPosition(Point(500, 600))
    slot0.setSize(Size(10000, 5000))
    slot0.setPropertyValue("Graphic", _graphic(10000, 5000))
    page = _DrawPage([slot0])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    # Ratio change: 10000x5000 (2:1) to 50x100 (1:2). Long side max(10000, 5000) = 10000.
    payload = _make_png_payload(50, 100)
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/change_aspect.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/change_aspect.png"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(1000, 2000), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(payload, code="plt.show()", doc=doc, image_index=0)

    # Revert target: _aspect_size(..., long_side=max(old_w, old_h)) on update path
    assert slot0.getPosition() == Point(500, 600)
    assert slot0.getSize() == Size(5000, 10000)

    # A box with max side 8000 (8000x4000) becomes (4000, 8000)
    slot0.setSize(Size(8000, 4000))
    slot0.setPropertyValue("Graphic", _graphic(10000, 5000))
    _set_tag(slot0, None)
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/change_aspect2.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/change_aspect2.png"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(1000, 2000), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(payload, code="plt.show()", doc=doc, image_index=0)

    assert slot0.getSize() == Size(4000, 8000)

    # A slot with ResizeWithCell=True does not resize on aspect change
    slot0.setPropertyValue("ResizeWithCell", True)
    slot0.setSize(Size(8000, 5000))
    slot0.setPropertyValue("Graphic", _graphic(10000, 5000))
    _set_tag(slot0, None)
    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/change_aspect3.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/change_aspect3.png"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(1000, 2000), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(payload, code="plt.show()", doc=doc, image_index=0)

    assert slot0.getSize() == Size(8000, 5000)


def test_new_shape_graphic_url_failure_leaves_draft_placeholder():
    """A new shape whose GraphicURL raises stays as a 10 cm x 6 cm placeholder without raising."""
    cell = CalcCellStub(col=0, row=0)
    page = _DrawPage([])
    sheet = _SheetStub(page)
    annotations = MagicMock()
    sheet.getAnnotations = lambda: annotations
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "", cell)

    def _set_fail(prop, _val):
        if prop == "GraphicURL":
            raise RuntimeError("graphic load error")

    shape.setPropertyValue = _set_fail
    doc.createInstance.return_value = shape

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/fail.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/fail.png"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(0, 0), Size(3000, 1000))),
    ):
        # A 1:2 image would be 5000 x 10000; the empty placeholder uses the default size.
        insert_image_result_on_sheet(_make_png_payload(50, 100), code="plt.show()", doc=doc, image_index=0)

    # Collabora parity: draft placeholder is left on the drawing page
    assert shape in page.shapes
    assert shape.getPosition() == Point(0, 0)
    # Revert target: _aspect_size(None) for a failed load in _place_new_plot
    assert shape.getSize() == Size(10000, 6000)
    annotations.insertNew.assert_not_called()


def test_new_shape_hides_when_formula_row_or_col_hidden():
    """A newly created shape anchored to a hidden row or column is set to Visible=False."""
    cell = CalcCellStub(col=0, row=0)

    # Case 1: Hidden row
    page_row = _DrawPage([])
    sheet_row = _SheetStub(page_row, rows_visible=False, cols_visible=True)
    doc_row = MagicMock(name="doc_row")
    doc_row.getCurrentController.return_value = None
    shape_row = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "", cell)
    doc_row.createInstance.return_value = shape_row

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet_row, cell, (0, 0)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/vis1.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/vis1.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(0, 0), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc_row, image_index=0)

    # Revert target: Visible assignment when _formula_axis_hidden is true
    assert shape_row.getPropertyValue("Visible") is False

    # Case 2: Hidden column
    page_col = _DrawPage([])
    sheet_col = _SheetStub(page_col, rows_visible=True, cols_visible=False)
    doc_col = MagicMock(name="doc_col")
    doc_col.getCurrentController.return_value = None
    shape_col = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "", cell)
    doc_col.createInstance.return_value = shape_col

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet_col, cell, (0, 0)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/vis2.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/vis2.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(0, 0), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc_col, image_index=0)

    assert shape_col.getPropertyValue("Visible") is False

    # Case 3: Visible row and column sets Visible True
    page_vis = _DrawPage([])
    sheet_vis = _SheetStub(page_vis, rows_visible=True, cols_visible=True)
    doc_vis = MagicMock(name="doc_vis")
    doc_vis.getCurrentController.return_value = None
    shape_vis = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "", cell)
    doc_vis.createInstance.return_value = shape_vis

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet_vis, cell, (0, 0)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/vis3.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/vis3.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(0, 0), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc_vis, image_index=0)

    assert shape_vis.getPropertyValue("Visible") is True


def test_reused_shape_preserves_visibility_across_recalc():
    """An existing shape preserves its Visible property across recalculations."""
    cell = CalcCellStub(col=0, row=0)
    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    shape.setPropertyValue("Visible", False)
    page = _DrawPage([shape])
    sheet = _SheetStub(page, rows_visible=True, cols_visible=True)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/vis4.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/vis4.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(0, 0), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, image_index=0)

    # Revert target: Visible must not be overwritten on existing shapes during recalc
    assert shape.getPropertyValue("Visible") is False


def test_new_shape_graphic_url_failure_without_target_cell_removes_shape():
    """When target_cell is None and GraphicURL fails, the placeholder shape is removed and ImageEgressError is raised."""
    page = _DrawPage([])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    sheets.getByIndex.return_value = sheet
    doc.getSheets.return_value = sheets

    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "", None)

    def _set_fail(prop, _val):
        if prop == "GraphicURL":
            raise RuntimeError("graphic load error")

    shape.setPropertyValue = _set_fail
    doc.createInstance.return_value = shape

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/fail.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/fail.svg"),
        pytest.raises(ImageEgressError, match="could not load image into graphic shape"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, doc=doc, image_index=0)

    # Revert target: removing the placeholder shape when target_cell is None
    assert shape not in page.shapes


def test_reanchored_slot_is_reused_via_owner_tag():
    """A slot Calc re-anchored to the cell under its new position is reused via its owner tag."""
    formula_cell = CalcCellStub(col=2, row=3)

    slot1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", CalcCellStub(col=7, row=3))
    _set_tag(slot1, "wa-plot:2,3:oldtag")
    page = _DrawPage([slot1])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, formula_cell, (3, 2)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/reanchored.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/reanchored.svg"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, image_index=1)

    # slot1 was reused rather than creating a new shape
    doc.createInstance.assert_not_called()
    assert slot1 in page.shapes
    assert len(page.shapes) == 1


def test_reanchored_slot_is_swept_via_owner_tag():
    """A stale slot Calc re-anchored to another cell is found via its owner tag and removed."""
    formula_cell = CalcCellStub(col=2, row=3)

    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, formula_cell)
    slot1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", CalcCellStub(col=7, row=3))
    _set_tag(slot1, "wa-plot:2,3:oldtag")
    page = _DrawPage([slot0, slot1])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, formula_cell, (3, 2)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/sweep.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/sweep.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(0, 0), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, image_index=0, image_count=1)

    assert slot0 in page.shapes
    assert slot1 not in page.shapes


def test_other_formulas_plot_anchored_at_formula_cell_not_matched():
    """A plot Calc anchored at this formula cell, but tagged for another formula, is left alone."""
    formula_cell = CalcCellStub(col=2, row=3)

    other_shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, formula_cell)
    _set_tag(other_shape, "wa-plot:5,5:oldtag")
    page = _DrawPage([other_shape])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    new_shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "", formula_cell)
    doc.createInstance.return_value = new_shape

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, formula_cell, (3, 2)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/new.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/new.svg"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(0, 0), Size(3000, 1000))),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, image_index=0)

    # Revert target: the tag deciding ownership in _shape_matches_target_cell. A new shape is
    # created for formula_cell rather than taking the other formula's plot.
    doc.createInstance.assert_called_once()
    assert other_shape in page.shapes
    assert new_shape in page.shapes


def test_untagged_anchored_plot_is_reused_and_tagged():
    """A plot with no tag is matched by its anchor and then gets its owner and digest tag."""
    formula_cell = CalcCellStub(col=1, row=2)
    payload = _make_png_payload(100, 50)

    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, formula_cell)
    page = _DrawPage([shape])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, formula_cell, (2, 1)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/match.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/match.png"),
    ):
        insert_image_result_on_sheet(payload, code="plt.show()", doc=doc, image_index=0)

    # Revert target: anchor fallback for untagged shapes and _record_plot_tag after the reload
    doc.createInstance.assert_not_called()
    assert _get_tag(shape) == _plot_tag(payload["data"], col=1, row=2)


def test_parse_and_format_plot_tag():
    """Verify wa-plot tag formatting and parsing only accepts wa-plot:<col>,<row>[:<sha>]."""
    assert _format_plot_tag(0, 0) == "wa-plot:0,0"
    assert _format_plot_tag(2, 5, "sha123") == "wa-plot:2,5:sha123"

    assert _parse_plot_tag("wa-plot:0,0") == ((0, 0), None)
    assert _parse_plot_tag("wa-plot:2,5:sha123") == ((2, 5), "sha123")

    # Revert target: rejection of bare sha or comma-less coordinates in _parse_plot_tag
    assert _parse_plot_tag("wa-plot:sha123") is None
    assert _parse_plot_tag("wa-plot") is None
    assert _parse_plot_tag("wa-plot:") is None
    assert _parse_plot_tag("wa-plot:invalid,coord:sha") is None
    assert _parse_plot_tag("not-a-plot-tag") is None


def test_new_shape_anchor_failure_chains_graphic_exception():
    """When graphic load fails and subsequent anchoring fails, graphic_exc is chained on ImageEgressError."""
    cell = CalcCellStub(col=0, row=0)
    page = _DrawPage([])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "", cell)

    graphic_error = RuntimeError("graphic load boom")

    def _set_prop(name: str, val: Any) -> None:
        if name == "GraphicURL":
            raise graphic_error

    shape.setPropertyValue = MagicMock(side_effect=_set_prop)
    doc.createInstance.return_value = shape

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/test.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/test.png"),
        patch("plugin.calc.python.image_egress.get_cell_geometry", side_effect=RuntimeError("geom boom")),
    ):
        with pytest.raises(ImageEgressError, match="could not anchor plot to the formula cell") as exc_info:
            insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, image_index=0)

    # Revert target: raise ImageEgressError from graphic_exc in _place_new_plot
    assert exc_info.value.__cause__ is graphic_error


def test_slot0_failure_still_sweeps_extra_slots():
    """When slot 0 raises ImageEgressError, extra slots >= image_count are still swept via finally."""
    cell = CalcCellStub(col=0, row=0)
    slot1 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, f"{_PLOT_SHAPE_NAME_PREFIX}_2", cell)
    page = _DrawPage([slot1])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.createInstance.return_value = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, "", cell)

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, cell, (0, 0)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/test.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/test.png"),
        # Slot 0 fails while it is being anchored.
        patch("plugin.calc.python.image_egress.get_cell_geometry", side_effect=RuntimeError("geom boom")),
    ):
        with pytest.raises(ImageEgressError):
            insert_image_result_on_sheet(
                _IMAGE_PAYLOAD, code="plt.show()", doc=doc, image_index=0, image_count=1
            )

    # Revert target: _drop_extra_plot_slots running in finally block of _insert_image_result_on_sheet_impl
    assert slot1 not in page.shapes


def test_reused_shape_skips_tag_write_when_tag_unchanged():
    """When the tag already matches, neither UserDefinedAttributes nor GraphicURL is written."""
    formula_cell = CalcCellStub(col=1, row=2)
    payload = _make_png_payload(100, 50)

    shape = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, formula_cell)
    _set_tag(shape, _plot_tag(payload["data"], col=1, row=2))
    page = _DrawPage([shape])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    set_calls: list[tuple[str, Any]] = []
    original_set_prop = shape.setPropertyValue

    def _track_set_prop(name: str, val: Any) -> None:
        set_calls.append((name, val))
        original_set_prop(name, val)

    shape.setPropertyValue = _track_set_prop

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(sheet, formula_cell, (2, 1)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/match.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/match.png"),
    ):
        insert_image_result_on_sheet(payload, code="plt.show()", doc=doc, image_index=0)

    # Revert target: digest match early return in _refresh_existing_plot
    assert [c for c in set_calls if c[0] in ("UserDefinedAttributes", "GraphicURL")] == []


class _ReanchoringShape(_AnchoredShape):
    """Stub shape that, like stock Calc, re-anchors to the cell under its new top-left corner.

    ScDrawView::adjustAnchoredPosition (drawvie3.cxx) runs SetCellAnchoredFromPosition on every
    move. Here every cell is 3000 x 1000, so the anchor is (X // 3000, Y // 1000).
    """

    def setPosition(self, pos: Any) -> None:
        super().setPosition(pos)
        self.props["Anchor"] = CalcCellStub(col=int(pos.X) // 3000, row=int(pos.Y) // 1000)


def test_three_reanchored_slots_chain_reuse_and_sweep():
    """Slots that Calc re-anchors to other cells still chain, are reused on recalc, and are swept."""
    cell = CalcCellStub(col=1, row=2)
    page = _DrawPage([])
    sheet = _SheetStub(page)
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None
    created: list[_ReanchoringShape] = []

    def _create_shape(service):
        shape = _ReanchoringShape(service, "", cell)
        created.append(shape)
        return shape

    doc.createInstance.side_effect = _create_shape
    payloads = [_make_png_payload(200, 100), _make_png_payload(100, 50), _make_png_payload(300, 150)]

    def _run(count: int) -> None:
        with (
            patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
            patch(
                "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
                return_value=(sheet, cell, (cell._row, cell._col)),
            ),
            patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/re.png"),
            patch("uno.systemPathToFileUrl", return_value="file:///tmp/re.png"),
            patch("plugin.calc.python.image_egress.get_cell_geometry", return_value=(Point(3000, 2000), Size(3000, 1000))),
        ):
            for i in range(count):
                insert_image_result_on_sheet(
                    payloads[i], code="plt.show()", doc=doc, image_index=i, image_count=count
                )

    _run(3)
    assert len(created) == 3
    slot0, slot1, slot2 = created
    # Calc moved slots 1 and 2 to the cells under them, away from the formula cell (1, 2).
    assert (slot1.getPropertyValue("Anchor")._col, slot2.getPropertyValue("Anchor")._col) == (4, 7)
    # Revert target: tag-decided ownership in _shape_matches_target_cell. Slot 2 sits at
    # slot 1's right edge, not on top of it.
    assert slot0.getPosition() == Point(3000, 2000)
    assert slot1.getPosition() == Point(13000, 2000)
    assert slot2.getPosition() == Point(23000, 2000)

    for shape in created:
        shape.props.pop("GraphicURL")
    _run(3)
    # Recalc with unchanged images creates nothing and reloads nothing.
    assert len(created) == 3
    assert all("GraphicURL" not in shape.props for shape in created)

    _run(2)
    assert slot0 in page.shapes and slot1 in page.shapes
    assert slot2 not in page.shapes


def test_existing_slot_user_stretch_kept_when_aspect_unchanged():
    """A box the user stretched keeps its size when new bytes have the old graphic's aspect."""
    cell = CalcCellStub(col=3, row=6)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot0.setPosition(Point(500, 600))
    slot0.setSize(Size(10000, 8000))
    slot0.setPropertyValue("Graphic", _graphic(10000, 5000))
    page = _DrawPage([slot0])
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(_SheetStub(page), cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/stretch.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/stretch.png"),
    ):
        insert_image_result_on_sheet(_make_png_payload(400, 200), code="plt.show()", doc=doc)

    assert slot0.getPropertyValue("GraphicURL") == "file:///tmp/stretch.png"
    # Revert target: comparing the old graphic's aspect, not the box's, in _refresh_existing_plot
    assert slot0.getSize() == Size(10000, 8000)
    assert slot0.getPosition() == Point(500, 600)


def test_rtl_existing_slot_aspect_change_keeps_top_right():
    """In RTL, an existing slot whose aspect changes keeps its top-right corner and long side."""
    cell = CalcCellStub(col=1, row=2)
    slot0 = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    slot0.setPosition(Point(-10000, 100))
    slot0.setSize(Size(10000, 5000))
    slot0.setPropertyValue("Graphic", _graphic(10000, 5000))
    page = _DrawPage([slot0])
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(_SheetStub(page, table_layout=1), cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/rtl.png"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/rtl.png"),
    ):
        insert_image_result_on_sheet(_make_png_payload(50, 100), code="plt.show()", doc=doc)

    # Revert target: the RTL setPosition in _refresh_existing_plot. The right edge stays at 0.
    assert slot0.getSize() == Size(5000, 10000)
    assert slot0.getPosition() == Point(-5000, 100)


def test_duplicate_shape_for_taken_slot_is_swept():
    """A second shape for an already-taken slot (e.g. a pasted copy) is removed; the first is kept."""
    cell = CalcCellStub(col=3, row=6)
    first = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    copy = _AnchoredShape(_DRAW_GRAPHIC_SERVICE, _PLOT_SHAPE_NAME_PREFIX, cell)
    _set_tag(copy, "wa-plot:3,6:old")
    page = _DrawPage([first, copy])
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value = None

    with (
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=MagicMock(name="front")),
        patch(
            "plugin.calc.python.image_egress.locate_formula_cell_in_doc",
            return_value=(_SheetStub(page), cell, (cell._row, cell._col)),
        ),
        patch("plugin.calc.python.image_egress.write_image_payload_to_temp", return_value="/tmp/dup.svg"),
        patch("uno.systemPathToFileUrl", return_value="file:///tmp/dup.svg"),
    ):
        insert_image_result_on_sheet(_IMAGE_PAYLOAD, code="plt.show()", doc=doc, image_count=1)

    # Revert target: the `slot_idx in taken` check in _drop_extra_plot_slots
    assert page.shapes == [first]
    assert first.getPropertyValue("GraphicURL") == "file:///tmp/dup.svg"
