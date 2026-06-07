# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted quant execution for Calc tools and Run Python Script."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.analysis_runner import calc_tool_context
from plugin.calc.venv_python import _resolve_python_data
from plugin.doc.document_helpers import is_calc, is_writer
from plugin.framework.client.quant_client import run_quant
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.quant_common import HELPER_NAMES

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def supports_quant_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Quant Helpers for *doc*."""
    if doc is None:
        return False
    try:
        return is_writer(doc) or is_calc(doc)
    except Exception:
        return False

def run_trusted_quant(
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
    """Fetch Calc data and run a trusted quant helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="QUANT_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="QUANT_ERROR")

    if not is_calc(doc) and not is_writer(doc):
        raise ToolExecutionError("Quant helpers require a Writer or Calc document.", code="QUANT_ERROR")

    dr = str(data_range).strip() if data_range else None
    
    py_data = None
    if dr or data is not None:
        tool_ctx = calc_tool_context(uno_ctx, doc)
        py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=data)
        if err:
            raise ToolExecutionError(err, code="QUANT_ERROR")
            
    # Some helpers like fetch_historical_data do not need py_data
    if name != "fetch_historical_data" and py_data is None:
        raise ToolExecutionError("Provide data_range or data for this quant helper", code="QUANT_ERROR")

    spec_params = params or {}

    context: dict[str, Any] = {}
    if is_calc(doc):
        try:
            from plugin.calc.bridge import CalcBridge
            context["sheet_name"] = CalcBridge(doc).get_active_sheet().getName()
        except Exception:
            pass
    if task_hint:
        context["task_hint"] = str(task_hint)
    if dr:
        context["range_a1"] = dr

    return run_quant(uno_ctx, name, spec_params, py_data, context=context or None)
