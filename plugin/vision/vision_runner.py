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
from plugin.vision.venv.vision import HELPER_NAMES
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
            _("Select an embedded image, then Run again."),
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
            _("Image '{name}' not found. Use list_images or leave image_name empty and select the graphic.").format(name=name),
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
