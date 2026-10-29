# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.calc_addin_data import _resolve_python_data
from plugin.calc.python.venv import RunVenvPythonScript
from plugin.tests.testing_utils import TestingFactory


def test_resolve_python_data_prefers_data_range():
    from plugin.scripting.calc_range import is_calc_range_payload

    ctx = MagicMock()
    ctx.doc = MagicMock()
    with patch("plugin.calc.bridge.CalcBridge"), patch("plugin.calc.inspector.CellInspector") as insp_cls:
        insp = insp_cls.return_value
        insp.read_range.return_value = [[{"value": 1}, {"value": 2}]]
        py_data, err = _resolve_python_data(ctx, data_range="A1:B1", data=[[99]])
        assert err is None
        assert is_calc_range_payload(py_data)
        assert py_data["data"] == [[1, 2]]
        assert py_data["address"] == "A1:B1"
        insp.read_range.assert_called_once_with("A1:B1")


def test_resolve_python_data_uses_data_param():
    from plugin.scripting.calc_range import is_calc_range_payload

    ctx = MagicMock()
    py_data, err = _resolve_python_data(ctx, data_range=None, data=[[1, 2]])
    assert err is None
    assert is_calc_range_payload(py_data)
    assert py_data["data"] == [[1, 2]]


@patch("plugin.calc.python.venv.run_code_in_user_venv")
def test_execute_passes_data(mock_run):
    from plugin.scripting.calc_range import is_calc_range_payload

    mock_run.return_value = {"status": "ok", "result": 1}
    tool = RunVenvPythonScript()
    ctx = TestingFactory.create_context(doc_type="calc")
    packed = {"__wa_payload__": "calc_range", "shape": [1, 1], "data": [[10]]}
    with patch("plugin.calc.python.venv.run_on_main", return_value=(packed, None)):
        out = tool.execute(ctx, code="result = float(np.sum(data))")
    assert out["status"] == "ok"
    mock_run.assert_called_once()
    assert is_calc_range_payload(mock_run.call_args.kwargs["data"])
    assert mock_run.call_args.kwargs["data"] == packed


@patch("plugin.framework.thread_guard.on_main_thread", return_value=False)
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.calc.python.venv.run_code_in_user_venv")
def test_run_venv_python_resolves_calc_data_on_main_thread(mock_run, mock_main_thread, mock_on_main):
    mock_run.return_value = {"status": "ok", "result": 1}
    call_order: list[str] = []

    def main_thread(fn, *args, **kwargs):
        call_order.append("main")
        return fn(*args, **kwargs)

    mock_main_thread.side_effect = main_thread

    tool = RunVenvPythonScript()
    ctx = TestingFactory.create_context(doc_type="calc")
    with patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([42], None)) as mock_resolve:
        out = tool.execute(ctx, code="result = data[0]", data_range="A1")

    assert out["status"] == "ok"
    assert call_order == ["main"]
    mock_resolve.assert_called_once()


@patch("plugin.calc.python.venv.run_code_in_user_venv")
def test_execute_writer_ignores_data(mock_run):
    mock_run.return_value = {"status": "ok", "result": 0}
    tool = RunVenvPythonScript()
    ctx = TestingFactory.create_context(doc_type="writer")
    with patch("plugin.calc.python.venv.run_on_main") as mock_resolve:
        out = tool.execute(ctx, code="result = 1", data=[[1, 2]], data_range="A1:A2")
    assert out["status"] == "ok"
    mock_resolve.assert_not_called()
    assert mock_run.call_args.kwargs["data"] is None


def test_get_parameters_calc_vs_writer():
    tool = RunVenvPythonScript()
    calc_props = tool.get_parameters("calc")["properties"]
    writer_props = tool.get_parameters("writer")["properties"]
    assert "data_range" in calc_props
    assert "data" in calc_props
    assert "data_range" not in writer_props
    assert "data" not in writer_props


def test_parameters_and_descriptions_by_doc_type():
    tool = RunVenvPythonScript()
    calc_props = tool.get_parameters("calc")["properties"]
    none_props = tool.get_parameters(None)["properties"]

    expected_data_range_desc = (
        "Optional A1 range(s) injected as CalcRange: one address → `data` is that range and `ranges == [data]`; "
        "two or more → `data` is the same list as `ranges`. Pass one address string, a comma/semicolon-separated string "
        "(e.g. 'A1:A10, C1:C10'), or an array of address strings."
    )
    assert calc_props["data_range"] == {"description": expected_data_range_desc, "type": "string"}
    assert none_props["data_range"]["description"] == "(Calc only) " + calc_props["data_range"]["description"]
    assert none_props["data_range"]["type"] == "string"

    calc_data_desc = "Optional 2D array of cell values as `data` (use data_range for bulk data; the host resolves addresses without putting values in the LLM context)."
    none_data_desc = "(Calc only) Optional 2D array of cell values as `data` (prefer data_range for bulk data)."
    assert calc_props["data"]["description"] == calc_data_desc
    assert none_props["data"]["description"] == none_data_desc
    assert none_props["data"]["description"] != "(Calc only) " + calc_props["data"]["description"]

    expected_calc_data = {"type": "array", "items": {"type": "array", "items": {}}, "description": calc_data_desc}
    expected_none_data = {"type": "array", "items": {"type": "array", "items": {}}, "description": none_data_desc}
    assert calc_props["data"] == expected_calc_data
    assert list(calc_props["data"].keys()) == list(expected_calc_data.keys())
    assert none_props["data"] == expected_none_data
    assert list(none_props["data"].keys()) == list(expected_none_data.keys())

    assert calc_props["data"]["items"] is not none_props["data"]["items"]

    for dt in ("writer", "draw", "impress"):
        props = tool.get_parameters(dt)["properties"]
        assert "data" not in props
        assert "data_range" not in props

    writer_desc = tool.get_description("writer")
    assert "does not inject spreadsheet" in writer_desc
    assert "the file" in writer_desc

    draw_desc = tool.get_description("draw")
    impress_desc = tool.get_description("impress")
    assert "slide/page" in draw_desc
    assert "slide/page" in impress_desc

    calc_desc = tool.get_description("calc")
    assert "Optional data_range" in calc_desc

    none_desc = tool.get_description(None)
    assert "In Calc, optional data_range" in none_desc
    assert "document tools" in none_desc


def test_calc_schema_includes_data_range():
    from plugin.framework.tool import ToolRegistry

    from plugin.tests.testing_utils import CalcDocStub

    registry = ToolRegistry(services={})
    registry.register(RunVenvPythonScript())
    schemas = registry.get_schemas("openai", doc=CalcDocStub(), active_domain="python")
    py_schema = next(s for s in schemas if s["function"]["name"] == "run_venv_python_script")
    props = py_schema["function"]["parameters"]["properties"]
    assert "data_range" in props
    assert "data" in props
    assert "timeout_sec" not in props


@patch("plugin.calc.python.venv.run_code_in_user_venv")
def test_execute_pins_ctx_doc_for_script_tools(mock_run):
    """wa.draw / wa.shape must see ctx.doc, not a shared worker namespace."""
    mock_run.return_value = {"status": "ok", "result": 1}
    tool = RunVenvPythonScript()
    ctx = TestingFactory.create_context(doc_type="draw")
    with (
        patch("plugin.scripting.session_manager.pin_script_document", return_value="doc:deck") as mock_pin,
        patch("plugin.scripting.session_manager.release_script_document") as mock_release,
    ):
        out = tool.execute(ctx, code="import writeragent as wa")
    assert out["status"] == "ok"
    mock_pin.assert_called_once_with(ctx.doc)
    assert mock_run.call_args.kwargs["script_session_id"] == "doc:deck"
    assert "session_id" not in mock_run.call_args.kwargs
    mock_release.assert_called_once_with("doc:deck")


@patch("plugin.calc.python.venv.run_code_in_user_venv")
def test_execute_releases_doc_pin_when_script_raises(mock_run):
    mock_run.side_effect = RuntimeError("worker down")
    tool = RunVenvPythonScript()
    ctx = TestingFactory.create_context(doc_type="draw")
    with (
        patch("plugin.scripting.session_manager.pin_script_document", return_value="doc:deck"),
        patch("plugin.scripting.session_manager.release_script_document") as mock_release,
        pytest.raises(RuntimeError, match="worker down"),
    ):
        tool.execute(ctx, code="import writeragent as wa")
    mock_release.assert_called_once_with("doc:deck")


def test_strip_image_payloads():
    from plugin.scripting.payload_codec import strip_image_payloads

    image = {"__wa_payload__": "image", "data": b"123", "format": "png"}

    # An image dict becomes "<image>"
    assert strip_image_payloads(image) == "<image>"

    # [1, None, image, 3] stays length 4 with None kept
    res_list = strip_image_payloads([1, None, image, 3])
    assert res_list == [1, None, "<image>", 3]
    assert len(res_list) == 4

    # [[1, 2]] stays [[1, 2]]
    assert strip_image_payloads([[1, 2]]) == [[1, 2]]

    # { "a": None, "b": image } keeps both keys
    res_dict = strip_image_payloads({"a": None, "b": image})
    assert res_dict == {"a": None, "b": "<image>"}


def test_execute_data_resolution_error_has_tool_error_code():
    tool = RunVenvPythonScript()
    ctx = TestingFactory.create_context(doc_type="calc")
    with patch("plugin.calc.python.venv.run_on_main", return_value=(None, "no such sheet")):
        out = tool.execute(ctx, code="result = 1", data_range="Nope.A1")
    assert out["status"] == "error"
    assert out["code"] == "DATA_RESOLUTION_ERROR"
    assert out["message"] == "no such sheet"

