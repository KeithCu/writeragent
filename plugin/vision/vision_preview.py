# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live OCR preview job for the Vision / OCR settings dialog."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from plugin.chatbot.module_config_dialog import PreviewImage
from plugin.doc.visual_helpers import graphic_objects_in_selection
from plugin.framework.errors import ToolExecutionError, best_effort
from plugin.framework.i18n import _
from plugin.framework.queue_executor import main
from plugin.framework.scratch_writer import new_blank_writer
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import get_active_document
from plugin.scripting.client import run_vision
from plugin.vision.vision_common import LOAD_MODELS_HELPER, detect_vision_input_format
from plugin.vision.vision_egress import vision_html_from_result
from plugin.vision.vision_runner import no_image_selected_error, run_trusted_vision, supports_vision_manual
from plugin.writer.html_import import insert_html_at_cursor
from plugin.writer.images.image_tools import graphic_from_bytes
from plugin.writer.page_png import render_writer_page_png

if TYPE_CHECKING:
    from plugin.framework.send_cancellation import SendCancellation

log = logging.getLogger(__name__)

_PREVIEW_PIXEL_WIDTH = 1600  # ~190 DPI on A4/Letter; the image control scales it, so a resize never re-renders
_PREVIEW_MARGIN = 500        # 1/100 mm (5 mm) so the bitmap is mostly content


@main_thread_only
def _preview_target(ctx: Any) -> tuple[Any, str]:
    doc = get_active_document(ctx)
    pairs = graphic_objects_in_selection(doc) if doc is not None and supports_vision_manual(doc) else []
    if not pairs:
        raise no_image_selected_error()
    return doc, pairs[0][0]  # first image, same discovery as Insert; "" (unnamed Calc image) falls back to the selection


def run_vision_preview(
    ctx: Any,
    values: dict[str, Any],
    cancel: SendCancellation,
    report: Callable[[str], None],
) -> PreviewImage:
    """Run OCR on the active document's first selected image and render page 1 for preview."""
    doc, image_name = main.call(_preview_target, ctx)

    def load_models(params: dict[str, Any], image: bytes) -> None:
        report(_("Loading models…"))
        spec = {"helper": LOAD_MODELS_HELPER, "params": {**params, "format": detect_vision_input_format(image, params)}}
        loaded = run_vision(ctx, spec, None, cancel=cancel)
        if loaded.get("status") == "error":
            # The OCR request below reports the real error (and may fall back to Paddle).
            log.info("Vision preview model load failed: %s", loaded.get("message"))
        report(_("Recognizing text…"))

    result = run_trusted_vision(
        ctx,
        doc,
        helper="extract_structure",
        params={**values, "image_name": image_name},
        cancel=cancel,
        on_exported=load_models,
    )
    html = vision_html_from_result(result)  # raises ToolExecutionError for error/empty results
    report(_("Rendering preview…"))
    summary = _("Done: {count} characters.").format(count=len(str(result.get("full_text") or "")))
    return main.call(render_preview_page, ctx, html, summary)


@main_thread_only
def render_preview_page(ctx: Any, html: str, summary: str) -> PreviewImage:
    """Render *html* into page 1 of a hidden Writer document and return a PreviewImage."""
    doc = new_blank_writer(ctx)
    if doc is None:
        raise ToolExecutionError(_("Could not create the preview document."), code="VISION_ERROR")
    try:
        cursor = doc.getText().createTextCursor()
        style = doc.getStyleFamilies().getByName("PageStyles").getByName(cursor.getPropertyValue("PageStyleName"))
        for side in ("LeftMargin", "RightMargin", "TopMargin", "BottomMargin"):
            style.setPropertyValue(side, _PREVIEW_MARGIN)
        insert_html_at_cursor(doc, ctx, cursor, html, apply_styles=False)  # the call Insert uses (vision_egress.py:183)
        height = round(_PREVIEW_PIXEL_WIDTH * style.getPropertyValue("Height") / style.getPropertyValue("Width"))
        png, reason = render_writer_page_png(doc, 0, pixel_size=(_PREVIEW_PIXEL_WIDTH, height))
        if png is None:
            raise ToolExecutionError(reason or _("Could not render the preview page."), code="VISION_ERROR")
        return PreviewImage(graphic_from_bytes(ctx, png), _PREVIEW_PIXEL_WIDTH, height, summary)
    finally:
        best_effort(lambda: doc.close(True), "vision preview: close scratch doc")
