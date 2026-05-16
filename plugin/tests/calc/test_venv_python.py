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
        assert py_data == [[1, 2]]
        insp.read_range.assert_called_once_with("A1:B1")


def test_resolve_python_data_uses_data_param():
    ctx = MagicMock()
    py_data, err = _resolve_python_data(ctx, data_range=None, data=[[1, 2]])
    assert err is None
    assert py_data == [[1, 2]]


@patch("plugin.calc.venv_python.run_code_in_user_venv")
def test_execute_passes_data(mock_run):
    mock_run.return_value = {"status": "ok", "result": 1}
    tool = RunVenvPythonScript()
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="calc", services=MagicMock())
    with patch("plugin.calc.venv_python._resolve_python_data", return_value=([[10]], None)):
        out = tool.execute(ctx, code="result = data[0][0]")
    assert out["status"] == "ok"
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["data"] == [[10]]
