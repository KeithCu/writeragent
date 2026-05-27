# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""LLM tool: run Python in the user-configured venv (see plugin/scripting/venv_worker.py)."""

from __future__ import annotations

import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any, ClassVar, cast

from plugin.calc.base import ToolCalcPythonBase
from plugin.calc.bridge import CalcBridge
from plugin.calc.calc_addin_data import check_python_data_size, finalize_python_data, pack_calc_data_for_wire, values_from_inspector_range
from plugin.calc.inspector import CellInspector
from plugin.framework.constants import PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.scripting.payload_codec import is_image_payload
from plugin.scripting.venv_worker import run_code_in_user_venv

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

_ALL_VENV_DOCS = [
    "com.sun.star.sheet.SpreadsheetDocument",
    "com.sun.star.text.TextDocument",
    "com.sun.star.drawing.DrawingDocument",
    "com.sun.star.presentation.PresentationDocument",
]

_PARAMETERS_CALC = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "Python / Numpy source. Set `result` to the return value (JSON-serializable).",
        },
        "data_range": {
            "type": "string",
            "description": "Optional A1 range (e.g. B1:B10); values are injected as variable `data`.",
        },
        "data": {
            "type": "array",
            "items": {"type": "array", "items": {}},
            "description": "Optional 2D array of cell values as `data` (use data_range to read from the sheet instead).",
        },
    },
    "required": ["code"],
}

_PARAMETERS_NON_CALC = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "Python / Numpy source. Set `result` to the return value (JSON-serializable).",
        },
    },
    "required": ["code"],
}

_DESCRIPTION_CALC = (
    "Run Python code. Set `result` to a JSON-serializable return value. "
    + PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
    + "Optional data_range (e.g. B1:B10) injects cell values as `data` (flat list for one row/column). "
    "Alternatively pass `data` directly after read_cell_range."
)

_DESCRIPTION_NON_CALC = (
    "Run Python code in the configured venv. Set `result` to a JSON-serializable return value. "
    + PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
    + "Use document tools to read or change the file; this tool does not inject spreadsheet `data`."
)


def _resolve_python_data(ctx: ToolContext, *, data_range: str | None, data: Any) -> tuple[Any | None, str | None]:
    """Return (py_data, error_message). Calc only; ``data_range`` wins over ``data`` when both set."""
    py_data: Any | None = None
    if data_range and str(data_range).strip():
        try:
            bridge = CalcBridge(ctx.doc)
            inspector = CellInspector(bridge)
            range_data = inspector.read_range(str(data_range).strip())
            py_data = values_from_inspector_range(range_data)
        except Exception as e:
            return None, f"Failed to read data_range: {e}"
    elif data is not None:
        py_data = finalize_python_data(data)

    if py_data is not None:
        size_err = check_python_data_size(py_data, max_cells=configured_python_max_data_cells(ctx.ctx))
        if size_err:
            return None, size_err
        py_data = pack_calc_data_for_wire(py_data)
    return py_data, None


class RunVenvPythonScript(ToolCalcPythonBase):
    """Registered once; visible in Writer/Calc/Draw specialized ``domain=python`` via ``specialized_cross_cutting``."""

    name = "run_venv_python_script"
    specialized_cross_cutting: ClassVar[bool] = True
    description = _DESCRIPTION_CALC
    parameters = _PARAMETERS_CALC
    uno_services = list(_ALL_VENV_DOCS)
    long_running = True

    def get_parameters(self, doc_type: str | None = None) -> dict | None:
        if doc_type == "calc":
            return _PARAMETERS_CALC
        return _PARAMETERS_NON_CALC

    def get_description(self, doc_type: str | None = None) -> str:
        if doc_type == "calc":
            return _DESCRIPTION_CALC
        return _DESCRIPTION_NON_CALC

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        code = str(kwargs.get("code", ""))
        if kwargs.get("timeout_sec") is not None:
            log.debug("run_venv_python_script: ignoring timeout_sec (user setting controls wall clock)")

        py_data = None
        if ctx.doc_type == "calc":
            data_range = kwargs.get("data_range")
            data = kwargs.get("data")
            py_data, err = _resolve_python_data(ctx, data_range=data_range, data=data)
            if err:
                return {"status": "error", "message": err}
        else:
            if kwargs.get("data_range") is not None or kwargs.get("data") is not None:
                log.debug(
                    "run_venv_python_script: ignoring data/data_range on doc_type=%s",
                    ctx.doc_type,
                )

        res = run_code_in_user_venv(
            ctx.ctx,
            code,
            data=py_data,
            active_domain=ctx.active_domain,
            python_tool_domain=ctx.python_tool_domain,
        )

        result = res.get("result")
        if res.get("status") == "ok" and is_image_payload(result):
            img = cast("dict[str, Any]", result)
            fmt = img.get("format", "png")
            suffix = ".svg" if fmt == "svg" else ".png"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(img["data"])
                tmp_path = tmp.name
            return {"status": "ok", "message": "Plot generated", "image_path": os.path.abspath(tmp_path)}

        return res
