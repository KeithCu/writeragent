# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted viz execution for Calc tools and Run Python Script."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.analysis_runner import calc_tool_context
from plugin.calc.venv_python import _resolve_python_data
from plugin.doc.document_helpers import is_calc, is_writer
from plugin.framework.client.viz_client import run_viz
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.viz_common import HELPER_NAMES

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def supports_viz_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Viz Helpers for *doc*."""
    if doc is None:
        return False
    try:
        return is_writer(doc) or is_calc(doc)
    except Exception:
        return False


def run_trusted_viz(
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
    """Fetch Calc data and run a trusted viz helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="VIZ_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="VIZ_ERROR")

    if not is_calc(doc) and not is_writer(doc):
        raise ToolExecutionError("Viz helpers require a Writer or Calc document.", code="VIZ_ERROR")

    dr = str(data_range).strip() if data_range else None
    if not dr and data is None:
        raise ToolExecutionError("Provide data_range or data", code="VIZ_ERROR")

    tool_ctx = calc_tool_context(uno_ctx, doc)
    py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=data)
    if err:
        raise ToolExecutionError(err, code="VIZ_ERROR")
    if py_data is None:
        raise ToolExecutionError("No data to plot", code="VIZ_ERROR")

    spec: dict[str, Any] = {"helper": name, "headers": bool(headers)}
    if isinstance(params, dict) and params:
        spec["params"] = params

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

    return run_viz(uno_ctx, spec, py_data, context=context or None)
