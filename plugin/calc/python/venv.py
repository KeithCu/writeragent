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
from typing import TYPE_CHECKING, Any, ClassVar

from plugin.calc.base import ToolCalcPythonBase
from plugin.calc.bridge import CalcBridge
from plugin.calc.calc_addin_data import check_python_data_size, finalize_python_data, pack_calc_data_for_wire, values_from_inspector_range
from plugin.calc.inspector import CellInspector
from plugin.framework.constants import PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
from plugin.scripting.import_policy import format_matplotlib_plot_hint
from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.scripting.payload_codec import find_image_payloads, write_image_payload_to_temp
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
            "description": "Python / Numpy source. Set `result` to the return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar).",
        },
        "data_range": {
            "type": "string",
            "description": "Optional A1 range (e.g. B1:B10); values are injected as variable `data`.",
        },
        "data": {
            "type": "array",
            "items": {"type": "array", "items": {}},
            "description": "Optional 2D array of cell values as `data` (use data_range for bulk data; the host resolves addresses without putting values in the LLM context).",
        },
    },
    "required": ["code"],
}

_PARAMETERS_NON_CALC = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "Python / Numpy source. Set `result` to the return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar).",
        },
    },
    "required": ["code"],
}

# Superset advertised when the target app is unknown (e.g. MCP discovery with no document
# open), so a caller heading for Calc still sees data_range. data_range/data are Calc-only.
_PARAMETERS_NEUTRAL = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "Python / Numpy source. Set `result` to the return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar).",
        },
        "data_range": {
            "type": "string",
            "description": "(Calc only) Optional A1 range (e.g. B1:B10); values are injected as variable `data`.",
        },
        "data": {
            "type": "array",
            "items": {"type": "array", "items": {}},
            "description": "(Calc only) Optional 2D array of cell values as `data` (prefer data_range for bulk data).",
        },
    },
    "required": ["code"],
}

_DESCRIPTION_CALC = (
    "Run Python code. Set `result` to a return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar). "
    + PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
    + "Optional data_range (e.g. 'Sheet1.B1:B10') injects cell values as `data`. "
    "The host reads the range on the main thread and sends shaped data over the efficient IPC path. "
    "For anything beyond tiny grids, use data_range (address) rather than passing values in the data parameter."
)

_DESCRIPTION_WRITER = (
    "Run Python code in the configured venv. Set `result` to a return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar). "
    + PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
    + "Use document tools to read or change the file; this tool does not inject spreadsheet `data`."
)

_DESCRIPTION_DRAW = (
    "Run Python code in the configured venv. Set `result` to a return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar). "
    + PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
    + "Use document tools to read or change the slide/page; this tool does not inject spreadsheet `data`."
)

# Used when the target app is unknown (e.g. discovery with no document open): covers both
# the Calc data_range path and the Writer/Draw no-injection path, to match the superset schema.
_DESCRIPTION_NEUTRAL = (
    "Run Python code in the configured venv. Set `result` to a return value (NumPy ndarray, Pandas DataFrame, list, dict, or scalar). "
    + PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE
    + "In Calc, optional data_range (e.g. 'Sheet1.B1:B10') injects cell values as `data`; "
    "in Writer or Draw/Impress use document tools to read or change content (no spreadsheet `data` injection)."
)


def _venv_tool_description(doc_type: str | None) -> str:
    if doc_type == "calc":
        base = _DESCRIPTION_CALC
    elif doc_type in ("draw", "impress"):
        base = _DESCRIPTION_DRAW
    elif doc_type is None:
        base = _DESCRIPTION_NEUTRAL
    else:
        base = _DESCRIPTION_WRITER
    hint = format_matplotlib_plot_hint(doc_type=doc_type)
    if hint:
        return f"{base} {hint}"
    return base


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


def resolve_python_data_on_main_thread(ctx: ToolContext, *, data_range: str | None, data: Any) -> tuple[Any | None, str | None]:
    """Marshal Calc range reads to the LO main thread (``is_async`` tools run on workers)."""
    from plugin.framework.queue_executor import execute_on_main_thread

    return execute_on_main_thread(_resolve_python_data, ctx, data_range=data_range, data=data)


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
        if doc_type is None:
            # App unknown (e.g. discovery with no document) -> advertise the superset so a
            # caller targeting Calc still sees data_range (marked Calc-only).
            return _PARAMETERS_NEUTRAL
        return _PARAMETERS_NON_CALC

    def get_description(self, doc_type: str | None = None) -> str:
        return _venv_tool_description(doc_type)

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
            py_data, err = resolve_python_data_on_main_thread(ctx, data_range=data_range, data=data)
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
        if res.get("status") == "ok":
            images = find_image_payloads(result)
            if images:
                img_paths = [write_image_payload_to_temp(img) for img in images]
                out: dict[str, Any] = {
                    "status": "ok",
                    "message": f"{len(images)} plot(s) generated",
                    "image_paths": img_paths,
                }
                if len(img_paths) == 1:
                    out["image_path"] = img_paths[0]
                else:
                    out["image_path"] = img_paths

                if ctx.doc_type == "calc":
                    from plugin.calc.python.image_egress import insert_image_result_on_sheet
                    from plugin.framework.queue_executor import execute_on_main_thread

                    for img in images:
                        execute_on_main_thread(insert_image_result_on_sheet, ctx.ctx, img)
                    out["message"] = f"{len(images)} plot(s) inserted on active sheet"
                    out["image_inserted"] = True
                return out

        return res
