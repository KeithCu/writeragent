# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Format and insert trusted viz / matplotlib image results into documents."""

from __future__ import annotations

from typing import Any

from plugin.doc.document_helpers import is_calc, is_draw, is_writer
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.scripting.image_payload import write_image_payload_to_temp
from plugin.scripting.payload_codec import is_image_payload
from plugin.scripting.viz_common import HELPER_NAMES


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
    return is_image_payload(image)


def extract_image_payload(value: Any) -> dict[str, Any] | None:
    """Return the ``__wa_payload__: image`` envelope from raw or viz-wrapped results."""
    if is_image_payload(value):
        return value
    if isinstance(value, dict):
        image = value.get("image")
        if is_image_payload(image):
            return image
    return None


def insert_image_payload_for_doc(
    ctx: Any,
    doc: Any,
    payload: dict[str, Any],
    *,
    title: str = "Plot",
) -> None:
    """Insert an image envelope into Calc, Writer, or Draw/Impress."""
    if is_calc(doc):
        from plugin.calc.python_image_egress import insert_image_result_on_sheet

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
    payload = extract_image_payload(result)
    if payload is None:
        raise ToolExecutionError(
            _("Viz helper returned no image payload."),
            code="VIZ_ERROR",
            details={"viz_result": result},
        )
    title = str(result.get("title") or result.get("helper") or "Plot")
    insert_image_payload_for_doc(ctx, doc, payload, title=title)


def try_insert_plot_result(ctx: Any, doc: Any, result_data: Any) -> bool:
    """Insert plot/image results when present. Returns True if insertion ran."""
    payload = extract_image_payload(result_data)
    if payload is None:
        return False
    title = "Plot"
    if isinstance(result_data, dict):
        title = str(result_data.get("title") or result_data.get("helper") or "Plot")
    insert_image_payload_for_doc(ctx, doc, payload, title=title)
    return True
