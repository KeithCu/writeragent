# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Viz helper templates, host RPC, and document egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.viz`` via ``__getattr__``.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.analysis_runner import calc_tool_context
from plugin.scripting._lazy_venv import make_getattr
from plugin.calc.python.venv import _resolve_python_data
from plugin.doc.document_helpers import is_calc, is_draw, is_writer
from plugin.scripting.client import run_viz as client_run_viz
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    build_helper_script_template,
    header_prefix,
    parse_helper_script_header,
)
from plugin.scripting.payload_codec import is_image_payload, find_image_payloads, write_image_payload_to_temp
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

# --- Constants & Common ---

HELPER_NAMES = frozenset(
    {
        "quick_plot",
        "plot_data",
        "correlation_heatmap",
        "time_series_plot",
    }
)

VIZ_HEADER_PREFIX = header_prefix("viz")

_SHIPPED_TEMPLATES = frozenset({"quick_plot", "correlation_heatmap", "time_series_plot"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "quick_plot": {},
    "correlation_heatmap": {"method": "pearson"},
    "time_series_plot": {"date_col": "Date", "value_col": "Amount"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "quick_plot": "Auto line/bar chart from numeric columns in the data range.",
    "correlation_heatmap": "Heatmap of pairwise correlations (matplotlib/seaborn).",
    "time_series_plot": "Line plot for date_col vs value_col.",
}

_VIZ_VENV_EXPORTS = frozenset(
    {
        "correlation_heatmap",
        "plot_data",
        "quick_plot",
        "run_viz",
        "time_series_plot",
    }
)

__getattr__ = make_getattr("viz", _VIZ_VENV_EXPORTS)


# --- Templates ---

VizScriptMeta = HelperScriptMeta


def _template_body(helper: str, params: dict[str, Any]) -> str:
    return build_helper_script_template(
        tag="viz",
        helper=helper,
        params=params,
        description=_HELPER_DESCRIPTIONS.get(helper, helper),
        style="run_import",
        import_module="writeragent.scripting.viz",
        run_name="run_viz",
        data_expr="data",
        context_expr="{}",
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    )


def get_viz_script_templates() -> dict[str, str]:
    """Return built-in viz helper scripts keyed by helper name."""
    return {
        helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {})))
        for helper in sorted(_SHIPPED_TEMPLATES)
        if helper in HELPER_NAMES
    }


def parse_viz_script_header(code: str) -> VizScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied viz script."""
    return parse_helper_script_header(code, tag="viz", helper_names=HELPER_NAMES)


# --- Runner ---

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

    return client_run_viz(uno_ctx, spec, py_data, context=context or None)


# --- Egress ---

def is_viz_result(value: Any) -> bool:
    """True when *value* matches the compact viz helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in HELPER_NAMES:
        return True
    image = value.get("image")
    if is_image_payload(image):
        return True
    if image is not None and bool(find_image_payloads(image)):
        return True
    return False


def extract_image_payload(value: Any) -> dict[str, Any] | None:
    """Return the ``__wa_payload__: image`` envelope from raw or viz-wrapped results."""
    if is_image_payload(value):
        return value
    if isinstance(value, dict):
        image = value.get("image")
        if is_image_payload(image):
            return image
    # Fallback to the first found image payload if there are multiple
    images = find_image_payloads(value)
    return images[0] if images else None


def insert_image_payload_for_doc(
    ctx: Any,
    doc: Any,
    payload: dict[str, Any],
    *,
    title: str = "Plot",
) -> None:
    """Insert an image envelope into Calc, Writer, or Draw/Impress."""
    if is_calc(doc):
        from plugin.calc.python.image_egress import insert_image_result_on_sheet

        insert_image_result_on_sheet(ctx, payload)
        return
    if is_writer(doc):
        from plugin.writer.images.image_tools import insert_image_at_locator

        path = write_image_payload_to_temp(payload)
        insert_image_at_locator(ctx, doc, path, title=title, description="WriterAgent plot")
        return
    if is_draw(doc):
        from plugin.writer.images.image_tools import insert_image

        path = write_image_payload_to_temp(payload)
        insert_image(ctx, doc, path, 400, 300, title=title, description="WriterAgent plot", add_to_gallery=False)
        return
    raise ToolExecutionError(_("Unsupported document type for plot insertion."), code="VIZ_ERROR")


def insert_viz_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any]) -> None:
    """Insert a viz helper result (image nested under ``image`` key)."""
    if result.get("status") == "error":
        code = str(result.get("code") or "VIZ_ERROR")
        message = str(result.get("message") or _("Viz helper failed."))
        raise ToolExecutionError(message, code=code, details={"viz_result": result})
    images = find_image_payloads(result)
    if not images:
        raise ToolExecutionError(
            _("Viz helper returned no image payload."),
            code="VIZ_ERROR",
            details={"viz_result": result},
        )
    title = str(result.get("title") or result.get("helper") or "Plot")
    for img in images:
        insert_image_payload_for_doc(ctx, doc, img, title=title)


def try_insert_plot_result(ctx: Any, doc: Any, result_data: Any) -> bool:
    """Insert plot/image results when present. Returns True if insertion ran."""
    images = find_image_payloads(result_data)
    if not images:
        return False
    title = "Plot"
    if isinstance(result_data, dict):
        title = str(result_data.get("title") or result_data.get("helper") or "Plot")
    for img in images:
        insert_image_payload_for_doc(ctx, doc, img, title=title)
    return True

