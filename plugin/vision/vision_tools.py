# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LLM/MCP tools for local vision OCR (trusted venv extract_structure)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from plugin.calc.base import ToolCalcVisionBase
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.framework.i18n import _
from plugin.vision.vision_runner import run_and_insert_vision_for_selection

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_VISION_DOC_TYPES = frozenset({"writer", "calc"})
_VISION_DOCS = ["com.sun.star.text.TextDocument", "com.sun.star.sheet.SpreadsheetDocument"]


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
            "insert_into_document": {"type": "boolean", "description": ("When true (default), insert a high-quality representation into the document. When false, return extracted content only.")},
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
            return self._tool_error(_("Vision OCR requires a Writer or Calc document."), code="VISION_ERROR")

        doc = ctx.doc
        insert_into_document = bool(kwargs.get("insert_into_document", True))
        params_raw = kwargs.get("params")
        params_dict: dict[str, Any] = dict(params_raw) if isinstance(params_raw, dict) else {}
        image_name = str(kwargs.get("image_name") or "").strip()
        if image_name:
            params_dict["image_name"] = image_name

        stop_checker = getattr(ctx, "stop_checker", None)

        try:
            result = run_and_insert_vision_for_selection(
                ctx.ctx,
                doc,
                helper="extract_structure",
                params=params_dict or None,
                insert_into_document=insert_into_document,
                stop_checker=stop_checker,
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
            # Partial OCR can insert some images then fail. Those fields used to
            # live only inside vision_result, so callers reading details missed
            # which image failed and what already landed. Lift them onto details.
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

        # Default images_processed to 0 only when the key is missing.
        # `or 1` turns a real 0 into 1.
        out: dict[str, Any] = {
            "status": "ok",
            "helper": "extract_structure",
            "full_text": str(result.get("full_text") or ""),
            "metrics": result.get("metrics") if isinstance(result.get("metrics"), dict) else {},
            "warnings": result.get("warnings") if isinstance(result.get("warnings"), list) else [],
            "inserted": bool(result.get("inserted")),
            "images_processed": int(result.get("images_processed", 0)),
            "message": str(result.get("message") or _("OCR complete.")),
        }
        names = result.get("image_names")
        if isinstance(names, list) and names:
            out["image_names"] = names
        return out
