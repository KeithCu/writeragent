# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from unittest.mock import MagicMock, patch

from plugin.calc.venv_python import RunVenvPythonScript, _resolve_python_data
from plugin.framework.tool import ToolContext


def test_resolve_python_data_prefers_data_range():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    with patch("plugin.calc.venv_python.CalcBridge") as bridge_cls, patch("plugin.calc.venv_python.CellInspector") as insp_cls:
        insp = insp_cls.return_value
        insp.read_range.return_value = [[{"value": 1}, {"value": 2}]]
        py_data, err = _resolve_python_data(ctx, data_range="A1:B1", data=[[99]])
        assert err is None
        assert py_data == [1, 2]
        insp.read_range.assert_called_once_with("A1:B1")


def test_resolve_python_data_uses_data_param():
    ctx = MagicMock()
    py_data, err = _resolve_python_data(ctx, data_range=None, data=[[1, 2]])
    assert err is None
    assert py_data == [1, 2]


@patch("plugin.calc.venv_python.run_code_in_user_venv")
def test_execute_passes_data(mock_run):
    mock_run.return_value = {"status": "ok", "result": 1}
    tool = RunVenvPythonScript()
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="calc", services=MagicMock())
    with patch("plugin.calc.venv_python._resolve_python_data", return_value=([10], None)):
        out = tool.execute(ctx, code="result = sum(data)")
    assert out["status"] == "ok"
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["data"] == [10]


@patch("plugin.calc.venv_python.run_code_in_user_venv")
def test_execute_writer_ignores_data(mock_run):
    mock_run.return_value = {"status": "ok", "result": 0}
    tool = RunVenvPythonScript()
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services=MagicMock())
    with patch("plugin.calc.venv_python._resolve_python_data") as mock_resolve:
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


def test_calc_schema_includes_data_range():
    from plugin.framework.tool import ToolRegistry

    registry = ToolRegistry(services={})
    registry.register(RunVenvPythonScript())
    mock_sheet = MagicMock()

    def supports(svc):
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    mock_sheet.supportsService = supports
    schemas = registry.get_schemas("openai", doc=mock_sheet, active_domain="python")
    py_schema = next(s for s in schemas if s["function"]["name"] == "run_venv_python_script")
    props = py_schema["function"]["parameters"]["properties"]
    assert "data_range" in props
    assert "data" in props
