# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LLM tools for local vision/OCR (trusted venv extract_text)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from plugin.calc.base import ToolCalcVisionBase
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.vision.vision_egress import insert_vision_result
from plugin.vision.vision_runner import run_trusted_vision

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_VISION_DOC_TYPES = frozenset({"writer", "calc"})
_VISION_DOCS = [
    "com.sun.star.text.TextDocument",
    "com.sun.star.sheet.SpreadsheetDocument",
]


class ExtractTextFromImage(ToolCalcVisionBase):
    """Run trusted extract_text OCR on an embedded graphic via the user venv."""

    name = "extract_text_from_image"
    specialized_cross_cutting: ClassVar[bool] = True
    description = (
        "OCR text from an embedded document image using local Docling/Paddle (Settings → Python venv). "
        "Leave image_name empty to use the currently selected graphic. "
        "By default inserts formatted HTML at the Writer cursor or Calc cell below the graphic anchor."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_name": {
                "type": "string",
                "description": "Graphic name from list_images (images domain). Empty = selected graphic.",
            },
            "insert_into_document": {
                "type": "boolean",
                "description": "When true (default), insert OCR HTML into the document. When false, return text only.",
            },
            "params": {
                "type": "object",
                "description": "Optional vision helper overrides (engine, lang, ocr_backend, …).",
            },
        },
        "required": [],
    }
    uno_services = _VISION_DOCS
    long_running = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Sub-agent / async tools run off the UI thread; use ctx.doc_type (no UNO) here.
        # run_trusted_vision and insert_vision_result are marshaled to the main thread below.
        if ctx.doc_type not in _VISION_DOC_TYPES:
            return self._tool_error(
                _("Vision OCR requires a Writer or Calc document."),
                code="VISION_ERROR",
            )

        doc = ctx.doc
        insert_into_document = bool(kwargs.get("insert_into_document", True))
        params_raw = kwargs.get("params")
        params_dict: dict[str, Any] = dict(params_raw) if isinstance(params_raw, dict) else {}
        image_name = str(kwargs.get("image_name") or "").strip()
        if image_name:
            params_dict["image_name"] = image_name

        def _run() -> dict[str, Any]:
            return run_trusted_vision(ctx.ctx, doc, helper="extract_text", params=params_dict or None)

        try:
            result = execute_on_main_thread(_run)
        except ToolExecutionError as exc:
            return self._tool_error(str(exc), code=getattr(exc, "code", "VISION_ERROR"))
        except Exception as exc:
            return self._tool_error(f"Vision OCR failed: {exc}", code="VISION_ERROR")

        if result.get("status") == "error":
            code = str(result.get("code") or "VISION_ERROR")
            message = str(result.get("message") or "Vision helper failed.")
            return self._tool_error(message, code=code, vision_result=result)

        inserted = False
        if insert_into_document:
            try:
                execute_on_main_thread(lambda: insert_vision_result(ctx.ctx, doc, result, params=params_dict or None))
                inserted = True
            except ToolExecutionError as exc:
                return self._tool_error(str(exc), code=getattr(exc, "code", "VISION_ERROR"), vision_result=result)
            except Exception as exc:
                return self._tool_error(f"Failed to insert OCR result: {exc}", code="VISION_ERROR", vision_result=result)

        full_text = str(result.get("full_text") or "")
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        message = _("OCR complete.") if inserted else _("OCR complete (text returned only; not inserted).")
        return {
            "status": "ok",
            "helper": "extract_text",
            "full_text": full_text,
            "metrics": metrics,
            "warnings": warnings,
            "inserted": inserted,
            "message": message,
        }
