# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Viz helper templates, host RPC, and document egress (LO host).

A viz result is ``{status, helper, title, image}``; the host inserts every image
envelope found in it, not only a top-level ``image`` key. ``status="error"`` is
raised with ``code``/``message``, never inserted.

Compute is lazy-loaded from ``plugin.scripting.venv.viz`` via ``__getattr__``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, cast

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.doc.doc_type import is_calc, is_draw, is_writer
from plugin.scripting.client import run_viz as client_run_viz
from plugin.scripting.payload_codec import (
    find_image_payloads,
    is_image_payload,
    is_multi_data,
    strip_image_payloads,
    write_image_payload_to_temp,
)
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

from plugin.scripting.calc_functions_common import VIZ_HELPER_NAMES as HELPER_NAMES

_DRAW_PLOT_SIZE = (400, 300)

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "quick_plot": {},
    "correlation_heatmap": {"method": "pearson"},
    "time_series_plot": {"date_col": "Date", "value_col": "Amount"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "quick_plot": "Auto line/bar chart of one numeric column in the data range.",
    "correlation_heatmap": "Heatmap of pairwise correlations (matplotlib/seaborn).",
    "time_series_plot": "Line plot for date_col vs value_col.",
}

_VIZ_VENV_EXPORTS = frozenset(HELPER_NAMES) | {"run_viz"}

install_lazy_exports(globals(), "viz", _VIZ_VENV_EXPORTS)


# --- Templates ---

from plugin.scripting.helper_domain import DomainFacadeConfig, make_template_api, run_trusted_calc_data_helper


_API = make_template_api(
    DomainFacadeConfig(
        tag="viz",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.viz",
        run_name="run_viz",
        shipped_templates=HELPER_NAMES - {"plot_data"},
        data_expr="data",
        leading_data=True,
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    )
)

get_viz_script_templates = _API.get_templates
parse_viz_script_header = _API.parse_header


# --- Runner ---


def run_trusted_viz(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch Calc data and run a trusted viz helper in the user venv."""
    return run_trusted_calc_data_helper(
        uno_ctx,
        doc,
        helper=helper,
        helper_names=HELPER_NAMES,
        error_code="VIZ_ERROR",
        empty_data_message="No data to plot",
        client_run=client_run_viz,
        require_calc_or_writer=True,
        **kwargs,
    )


# --- Egress ---

def is_viz_result(value: Any) -> bool:
    """True when *value* matches the compact viz helper result contract."""
    # A nested image inside some other helper used to claim the whole result
    # as a plot, so later domains never ran.
    if not isinstance(value, dict) or "status" not in value:
        return False
    helper = value.get("helper")
    return (isinstance(helper, str) and helper in HELPER_NAMES) or is_image_payload(value.get("image"))


def _unlink_plot_temp(path: str) -> None:
    # What was wrong: Writer and Draw plot insert wrote a temp file and returned,
    # so each insert leaked one SVG/PNG.
    # How: GraphicURL embeds before it returns, and a path under the temp directory
    # is not linked (_should_link_image_path).
    # Why this fixes it: this function owns the path it just wrote, so finally
    # unlinks it even when insert raises. Calc unlinks in image_egress.
    # PlotDataTool's returned image_path is a different call and stays.
    try:
        os.unlink(path)
    except OSError:
        log.debug("failed to unlink plot temp %s", path, exc_info=True)


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

        # Pass the same document =PY() already read. Omitting doc= wrote the
        # chart to desktop.getCurrentComponent(), so image egress fell back to
        # the front window (MCP document_url, or a second open workbook).
        insert_image_result_on_sheet(ctx, payload, doc=doc)
        return
    writer = is_writer(doc)
    if not writer and not is_draw(doc):
        raise ToolExecutionError(_("Unsupported document type for plot insertion."), code="VIZ_ERROR")
    from plugin.framework.uno_context import product_display_name
    from plugin.writer.images.image_tools import insert_image, insert_image_at_locator

    path = write_image_payload_to_temp(payload)
    description = f"{product_display_name(ctx)} plot"
    try:
        if writer:
            insert_image_at_locator(ctx, doc, path, title=title, description=description)
        else:
            insert_image(ctx, doc, path, *_DRAW_PLOT_SIZE, title=title, description=description, add_to_gallery=False)
    finally:
        _unlink_plot_temp(path)


def insert_viz_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any]) -> int:
    """Insert every image payload found in a viz helper result."""
    if result.get("status") == "error":
        code = str(result.get("code") or "VIZ_ERROR")
        message = str(result.get("message") or _("Viz helper failed."))
        # details travel verbatim into the chat error envelope; keep image bytes out.
        raise ToolExecutionError(message, code=code, details={"viz_result": strip_image_payloads(result)})
    images = find_image_payloads(result)
    if not images:
        raise ToolExecutionError(
            _("Viz helper returned no image payload."),
            code="VIZ_ERROR",
            details={"viz_result": strip_image_payloads(result)},
        )
    title = str(result.get("title") or result.get("helper") or "Plot")
    for img in images:
        insert_image_payload_for_doc(ctx, doc, img, title=title)
    return len(images)


def _images_claimed_as_plot(result_data: Any) -> list[dict[str, Any]]:
    """Images this turn should insert as a plot, not every nested picture."""
    if is_image_payload(result_data):
        return [result_data]
    if is_multi_data(result_data):
        items = result_data["items"]
        if all(is_image_payload(item) for item in items):
            return list(items)
        return []
    if isinstance(result_data, dict):
        helper = result_data.get("helper")
        if isinstance(helper, str) and helper in HELPER_NAMES:
            return find_image_payloads(result_data)
        image = result_data.get("image")
        if is_image_payload(image):
            return [cast("dict[str, Any]", image)]
    return []


def try_insert_plot_result(ctx: Any, doc: Any, result_data: Any) -> bool:
    """Insert plot/image results when present. Returns True if insertion ran."""
    images = _images_claimed_as_plot(result_data)
    if not images:
        return False
    title = "Plot"
    if isinstance(result_data, dict):
        title = str(result_data.get("title") or result_data.get("helper") or "Plot")
    for img in images:
        insert_image_payload_for_doc(ctx, doc, img, title=title)
    return True

