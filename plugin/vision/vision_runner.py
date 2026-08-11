# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted vision execution for Run Python Script (Writer graphic export + venv RPC)."""

from __future__ import annotations

import base64
from typing import Any

from plugin.vision.vision_common import merge_vision_params

from plugin.doc.document_helpers import is_calc, is_writer
from plugin.scripting.client import run_vision
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.vision.vision_common import HELPER_NAMES
from plugin.doc.visual_helpers import get_graphic_object_by_name as _get_graphic_object
from plugin.writer.images.image_tools import export_graphic_object_to_bytes, get_selected_image_base64


def supports_vision_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Vision Helpers for *doc*."""
    if doc is None:
        return False
    try:
        return is_writer(doc) or is_calc(doc)
    except Exception:
        return False


def get_selected_image_bytes(ctx: Any, doc: Any) -> bytes:
    """Export the currently selected embedded graphic as raw PNG bytes."""
    b64 = get_selected_image_base64(doc, ctx)
    if not b64:
        raise ToolExecutionError(
            _("Select an embedded image (or a range containing images), then Run again."),
            code="NO_IMAGE_SELECTED",
        )
    return base64.b64decode(b64)


def resolve_vision_image_bytes(ctx: Any, doc: Any, *, image_name: str | None = None) -> bytes:
    """Export PNG bytes from *image_name* or the current graphic selection."""
    name = str(image_name or "").strip()
    if not name:
        return get_selected_image_bytes(ctx, doc)

    graphic_obj = _get_graphic_object(doc, name)
    if graphic_obj is None:
        raise ToolExecutionError(
            _("Image '{name}' not found. Use image_list or leave image_name empty and select the graphic.").format(name=name),
            code="IMAGE_NOT_FOUND",
            details={"image_name": name},
        )
    png_bytes = export_graphic_object_to_bytes(ctx, graphic_obj)
    if not png_bytes:
        raise ToolExecutionError(
            _("Image '{name}' could not be exported.").format(name=name),
            code="IMAGE_NOT_FOUND",
            details={"image_name": name},
        )
    return png_bytes


def _resolve_locale_language(ctx: Any, doc: Any, graphic_obj: Any) -> str:
    # 1. Try to get CharLocale from the graphic object itself
    if graphic_obj is not None:
        try:
            locale = graphic_obj.getPropertyValue("CharLocale")
            if locale and getattr(locale, "Language", None):
                return str(locale.Language).lower()
        except Exception:
            pass

    # 2. Try to get CharLocale from the current selection/cursor
    try:
        selection = doc.CurrentController.Selection
        if selection:
            if hasattr(selection, "getCount") and selection.getCount() > 0:
                sel_obj = selection.getByIndex(0)
            else:
                sel_obj = selection
            locale = sel_obj.getPropertyValue("CharLocale")
            if locale and getattr(locale, "Language", None):
                return str(locale.Language).lower()
    except Exception:
        pass

    # 3. Fall back to LibreOffice UI locale
    try:
        from plugin.framework.i18n import get_lo_locale
        lo_locale = get_lo_locale(ctx)
        if lo_locale:
            return lo_locale.split("_")[0].split("-")[0].lower()
    except Exception:
        pass

    return "en"


def run_trusted_vision(
    ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export graphic bytes and run a trusted vision helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="VISION_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="VISION_ERROR")

    params_dict = merge_vision_params(ctx, dict(params) if isinstance(params, dict) else None)
    image_name = params_dict.get("image_name")

    graphic_obj = None
    if image_name:
        graphic_obj = _get_graphic_object(doc, str(image_name))
    else:
        try:
            selection = doc.CurrentController.Selection
            if selection:
                if hasattr(selection, "getCount") and selection.getCount() > 0:
                    graphic_obj = selection.getByIndex(0)
                else:
                    graphic_obj = selection
        except Exception:
            pass

    if not params_dict.get("lang"):
        params_dict["lang"] = _resolve_locale_language(ctx, doc, graphic_obj)

    png_bytes = resolve_vision_image_bytes(ctx, doc, image_name=str(image_name) if image_name is not None else None)
    spec: dict[str, Any] = {"helper": name, "params": params_dict}
    source = "graphic_name" if str(image_name or "").strip() else "selection"
    context: dict[str, Any] = {"source": source}
    if source == "graphic_name":
        context["image_name"] = str(image_name).strip()
    return run_vision(ctx, spec, png_bytes, context=context)


def run_and_insert_vision_for_selection(
    ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    insert_into_document: bool = True,
) -> dict[str, Any]:
    """OCR each graphic in the selection (or one named image) and optionally insert.

    Discovers named graphics while the selection is intact, then OCRs and inserts
    by ``image_name`` so a text-range selection is collapsed before any edit and
    intervening text is never replaced.
    """
    from plugin.doc.visual_helpers import graphic_objects_in_selection
    from plugin.vision.vision_egress import insert_vision_result

    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="VISION_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="VISION_ERROR")

    params_dict = merge_vision_params(ctx, dict(params) if isinstance(params, dict) else None)
    explicit_name = str(params_dict.get("image_name") or "").strip()

    if explicit_name:
        target_names = [explicit_name]
    else:
        # Capture names before any insert collapses/clears the selection.
        pairs = graphic_objects_in_selection(doc)
        target_names = [n for n, _unused in pairs if n]
        if not target_names:
            raise ToolExecutionError(
                _("Select an embedded image (or a range containing images), then Run again."),
                code="NO_IMAGE_SELECTED",
            )

    results: list[dict[str, Any]] = []
    for image_name in target_names:
        per_params = dict(params_dict)
        per_params["image_name"] = image_name
        result = run_trusted_vision(ctx, doc, helper=name, params=per_params)
        if result.get("status") == "error":
            return result
        if insert_into_document:
            # prepare_vision_writer_insert collapses any range selection before HTML import.
            insert_vision_result(ctx, doc, result, params=per_params)
        results.append(result)

    full_parts = [str(r.get("full_text") or "") for r in results]
    warnings: list[Any] = []
    for r in results:
        w = r.get("warnings")
        if isinstance(w, list):
            warnings.extend(w)
    metrics: dict[str, Any] = {"images_processed": len(results)}
    if len(results) == 1:
        single_metrics = results[0].get("metrics")
        if isinstance(single_metrics, dict):
            metrics.update(single_metrics)

    inserted = bool(insert_into_document)
    if inserted:
        message = (
            _("OCR complete ({count} images).").format(count=len(results))
            if len(results) > 1
            else _("OCR complete.")
        )
    else:
        message = _("OCR complete (text returned only; not inserted).")

    return {
        "status": "ok",
        "helper": name,
        "full_text": "\n\n".join(p for p in full_parts if p) if any(full_parts) else "\n\n".join(full_parts),
        "html": results[-1].get("html") if len(results) == 1 else "",
        "metrics": metrics,
        "warnings": warnings,
        "inserted": inserted,
        "images_processed": len(results),
        "image_names": list(target_names),
        "message": message,
        "results": results if len(results) > 1 else None,
    }
