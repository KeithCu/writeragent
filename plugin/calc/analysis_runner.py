# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted analysis execution for Calc tools and Run Python Script."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from plugin.calc.address_utils import index_to_column
from plugin.calc.bridge import CalcBridge
from plugin.doc.document_helpers import is_calc
from plugin.scripting.client import run_analysis
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.analysis import HELPER_NAMES

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def calc_tool_context(uno_ctx: Any, doc: Any) -> ToolContext:
    """Minimal ToolContext-like object for range reads on the main thread."""
    from types import SimpleNamespace

    return cast(
        "ToolContext",
        SimpleNamespace(ctx=uno_ctx, doc=doc, doc_type="calc" if is_calc(doc) else None, active_domain=None),
    )


def calc_selection_to_a1(doc: Any) -> str | None:
    """Format the current Calc selection as a sheet-qualified A1 address."""
    if doc is None or not is_calc(doc):
        return None
    try:
        controller = doc.getCurrentController()
        selection = controller.getSelection()
        if selection is None or not hasattr(selection, "getRangeAddress"):
            return None
        addr = selection.getRangeAddress()
        bridge = CalcBridge(doc)
        sheet = bridge.get_active_sheet()
        sheet_name = str(sheet.getName())
        start = f"{index_to_column(addr.StartColumn)}{addr.StartRow + 1}"
        end = f"{index_to_column(addr.EndColumn)}{addr.EndRow + 1}"
        cell_part = start if start == end else f"{start}:{end}"
        if " " in sheet_name or "." in sheet_name:
            return f"'{sheet_name}'.{cell_part}"
        return f"{sheet_name}.{cell_part}"
    except Exception:
        return None


def run_trusted_analysis(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    data_range: str | None = None,
    data: Any = None,
    headers: bool = True,
    task_hint: str | None = None,
) -> dict[str, Any]:
    """Fetch Calc data and run a trusted helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="ANALYSIS_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="ANALYSIS_ERROR")

    dr = str(data_range).strip() if data_range else None
    if not dr and data is None:
        raise ToolExecutionError("Provide data_range or data", code="ANALYSIS_ERROR")

    from plugin.calc.python.venv import _resolve_python_data

    tool_ctx = calc_tool_context(uno_ctx, doc)
    py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=data)
    if err:
        raise ToolExecutionError(err, code="ANALYSIS_ERROR")
    if py_data is None:
        raise ToolExecutionError("No data to analyze", code="ANALYSIS_ERROR")

    spec: dict[str, Any] = {"helper": name, "headers": bool(headers)}
    if isinstance(params, dict) and params:
        spec["params"] = params

    context: dict[str, Any] = {}
    try:
        bridge = CalcBridge(doc)
        context["sheet_name"] = bridge.get_active_sheet().getName()
    except Exception:
        pass
    if task_hint:
        context["task_hint"] = str(task_hint)
    if dr:
        context["range_a1"] = dr

    return run_analysis(uno_ctx, spec, py_data, context=context or None)
