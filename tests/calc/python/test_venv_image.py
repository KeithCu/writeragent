# WriterAgent - tests for run_venv_python_script image handling

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.python.image_egress import _shape_anchor_matches_cell, insert_image_result_on_sheet
from tests.testing_utils import CalcCellStub
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
    assert out["image_inserted"] is True
    assert out["image_path"] == "/tmp/plot.svg"
    assert "active sheet" in out["message"]
    assert main_thread.call_count == 1
    insert.assert_called_once_with(ctx.ctx, _IMAGE_PAYLOAD)


def test_writer_image_result_returns_path_only():
    tool = RunVenvPythonScript()
    ctx = MagicMock()
    ctx.doc_type = "writer"
    ctx.ctx = MagicMock()

    with (
        patch("plugin.calc.python.venv.run_code_in_user_venv", return_value={"status": "ok", "result": _IMAGE_PAYLOAD}),
        patch("plugin.calc.python.venv.write_image_payload_to_temp", return_value="/tmp/plot.svg"),
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet") as insert,
        patch("plugin.scripting.config_limits.configured_python_max_data_cells", return_value=10000),
    ):
        out = tool.execute(ctx, code="import matplotlib.pyplot as plt\nplt.plot([1])")

    assert out["status"] == "ok"
    assert out.get("image_inserted") is None
    assert out["image_path"] == "/tmp/plot.svg"
    insert.assert_not_called()


def test_insert_image_result_on_sheet_none_doc_safely_ignored():
    """insert_image_result_on_sheet handles None doc without AttributeError."""
    ctx = MagicMock()
    with patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", return_value=None):
        # Should not raise
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
    """When called off main thread, insert_image_result_on_sheet marshals to main thread."""
    ctx = MagicMock()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.execute_on_main_thread") as exec_main,
    ):
        insert_image_result_on_sheet(ctx, _IMAGE_PAYLOAD)

    assert exec_main.call_count == 1


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
