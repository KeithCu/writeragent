# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LLM/MCP tools for local vision OCR (trusted venv extract_structure)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from plugin.calc.base import ToolCalcVisionBase
from plugin.draw.base import DRAW_AND_IMPRESS
from plugin.framework.config_schema import as_bool
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.framework.i18n import _
from plugin.vision.vision_runner import run_and_insert_vision_for_selection

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_VISION_DOC_TYPES = frozenset({"writer", "calc", "draw", "impress"})
_VISION_DOCS = [
    "com.sun.star.text.TextDocument",
    "com.sun.star.sheet.SpreadsheetDocument",
    *DRAW_AND_IMPRESS,
]


class ExtractStructureFromImage(ToolCalcVisionBase):
    """Run trusted extract_structure OCR on an embedded graphic via the user venv."""

    name: str | None = "extract_structure_from_image"
    specialized_cross_cutting: ClassVar[bool] = True
    description: str = (
        "Extract text and structure (layout, tables, etc.) from embedded document image(s). "
        "Leave image_name empty to use the currently selected graphic, or a Writer selection that "
        "contains multiple images (intervening text is ignored). "
        "By default inserts a high-quality representation after each graphic (Writer) or below the Calc anchor."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "image_name": {"type": "string", "description": "Graphic name from image_list (images domain). Empty = selected graphic(s)."},
            "insert_into_document": {
                "type": "boolean",
                "description": (
                    "When true (default), insert a high-quality representation into the document. "
                    "When false, return extracted content only. Draw and Impress refuse true."
                ),
            },
            "params": {"type": "object", "description": "Optional vision helper overrides (engine, lang, ocr_backend, …)."},
        },
        "required": [],
    }
    uno_services: list[str] | None = _VISION_DOCS
    long_running: bool = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Sub-agent / async tools run off the UI thread; use ctx.doc_type (no UNO) here.
        # Do not marshal this whole call: venv OCR can take ~120s and would freeze the UI.
        # run_and_insert_vision_for_selection marshals only the UNO export/insert parts.
        if ctx.doc_type not in _VISION_DOC_TYPES:
            return self._tool_error(
                _("Vision OCR requires a Writer, Calc, Draw, or Impress document."),
                code="VISION_ERROR",
            )

        if ctx.doc_type in ("draw", "impress"):
            if "insert_into_document" in kwargs and as_bool(kwargs["insert_into_document"]):
                return self._tool_error(
                    _("Draw and Impress return vision text only; omit insert_into_document or set it to false."),
                    code="VISION_ERROR",
                )
            insert_into_document = False
        else:
            insert_into_document = as_bool(kwargs.get("insert_into_document", True))
        params_raw = kwargs.get("params")
        params_dict: dict[str, Any] = dict(params_raw) if params_raw is not None else {}
        image_name = str(kwargs.get("image_name") or "").strip()
        if image_name:
            params_dict["image_name"] = image_name

        doc = ctx.doc
        scope = ctx.send_cancellation

        try:
            result = run_and_insert_vision_for_selection(
                ctx.ctx,
                doc,
                helper="extract_structure",
                params=params_dict or None,
                insert_into_document=insert_into_document,
                cancel=scope,
            )
        except DocumentDisposedError:
            raise
        except ToolExecutionError as exc:
            return self._tool_error(str(exc), code=getattr(exc, "code", "VISION_ERROR"))
        except Exception as exc:
            return self._tool_error(f"Vision OCR failed: {exc}", code="VISION_ERROR")

        if result.get("status") == "error":
            code = str(result.get("code") or "VISION_ERROR")
            message = str(result.get("message") or "Vision helper failed.")
            # Partial OCR errors lift image progress onto details for callers that skip vision_result.
            partial_fields: dict[str, Any] = {}
            if result.get("partial"):
                partial_fields = {
                    "partial": True,
                    "images_processed": result.get("images_processed"),
                    "image_names": result.get("image_names"),
                    "failed_image": result.get("failed_image"),
                    "inserted": bool(result.get("inserted")),
                }
            return self._tool_error(message, code=code, vision_result=result, **partial_fields)

        return result
