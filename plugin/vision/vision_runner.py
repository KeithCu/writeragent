# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted vision execution for Run Python Script (Writer graphic export + venv RPC)."""

from __future__ import annotations

import base64
from typing import Any

from plugin.doc.doc_type import is_calc, is_writer
from plugin.doc.visual_helpers import get_graphic_object_by_name as _get_graphic_object
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed
from plugin.framework.i18n import _
from plugin.framework.queue_executor import SendCancelled, execute_on_main_thread
from plugin.scripting.client import run_vision
from plugin.vision.vision_common import (
    HELPER_NAMES,
    IMPLEMENTED_HELPERS,
    VISION_IMAGE_MAX_BYTES,
    merge_vision_params,
)
from plugin.writer.images.image_tools import export_graphic_object_to_bytes, get_selected_image_base64


def supports_vision_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Vision Helpers for *doc*."""
    if doc is None:
        return False
    try:
        return is_writer(doc) or is_calc(doc)
    except Exception:
        return False


def extract_vision_insert_kwargs(ctx: Any, code: str) -> dict[str, Any]:
    """Extract vision insertion kwargs (merged params) from script code."""
    from plugin.scripting.helper_domain import parse_run_import_call_spec

    call_spec = parse_run_import_call_spec(code, run_name="run_vision") or {}
    raw_params = call_spec.get("params") if isinstance(call_spec.get("params"), dict) else None
    return {"params": merge_vision_params(ctx, raw_params)}


def get_selected_image_bytes(ctx: Any, doc: Any) -> bytes:
    """Export the currently selected embedded graphic as raw PNG bytes."""
    b64 = get_selected_image_base64(doc, ctx)
    if not b64:
        raise ToolExecutionError(_("Select an embedded image (or a range containing images), then Run again."), code="NO_IMAGE_SELECTED")
    return base64.b64decode(b64)


def resolve_vision_image_bytes(ctx: Any, doc: Any, *, image_name: str | None = None, graphic_obj: Any = None) -> bytes:
    """Export PNG bytes from *image_name* or the current graphic selection."""
    name = str(image_name or "").strip()
    if not name:
        return get_selected_image_bytes(ctx, doc)

    if graphic_obj is None:
        graphic_obj = _get_graphic_object(doc, name)
    if graphic_obj is None:
        raise ToolExecutionError(_("Image '{name}' not found. Use image_list or leave image_name empty and select the graphic.").format(name=name), code="IMAGE_NOT_FOUND", details={"image_name": name})
    png_bytes = export_graphic_object_to_bytes(ctx, graphic_obj)
    if not png_bytes:
        raise ToolExecutionError(_("Image '{name}' could not be exported.").format(name=name), code="IMAGE_NOT_FOUND", details={"image_name": name})
    return png_bytes


def _validate_helper(helper: str) -> str:
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="VISION_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="VISION_ERROR")
    if name not in IMPLEMENTED_HELPERS:
        # HELPER_NAMES still lists Phase 4/5 helpers so copied script headers parse.
        # Exporting a graphic and RPC'ing them only to get UNKNOWN_HELPER from the worker
        # wasted the selection. Reject here.
        raise ToolExecutionError(f"Helper {name!r} is not implemented yet.", code="UNKNOWN_HELPER")
    return name


def _first_selected_object(doc: Any) -> Any:
    try:
        selection = doc.CurrentController.Selection
        if selection:
            if hasattr(selection, "getCount") and selection.getCount() > 0:
                return selection.getByIndex(0)
            return selection
    except Exception as exc:
        reraise_if_disposed(exc)
    return None


def _resolve_locale_language(ctx: Any, doc: Any, graphic_obj: Any) -> str:
    # 1. Try to get CharLocale from the graphic object itself
    if graphic_obj is not None:
        try:
            locale = graphic_obj.getPropertyValue("CharLocale")
            if locale and getattr(locale, "Language", None):
                return str(locale.Language).lower()
        except Exception as exc:
            reraise_if_disposed(exc)

    # 2. Try to get CharLocale from the current selection/cursor
    try:
        sel_obj = _first_selected_object(doc)
        if sel_obj is not None:
            locale = sel_obj.getPropertyValue("CharLocale")
            if locale and getattr(locale, "Language", None):
                return str(locale.Language).lower()
    except Exception as exc:
        reraise_if_disposed(exc)

    # 3. Fall back to LibreOffice UI locale
    try:
        from plugin.framework.i18n import get_lo_locale

        lo_locale = get_lo_locale(ctx)
        if lo_locale:
            return lo_locale.split("_")[0].split("-")[0].lower()
    except Exception as exc:
        reraise_if_disposed(exc)

    return "en"


def run_trusted_vision(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Export graphic bytes and run a trusted vision helper in the user venv."""
    name = _validate_helper(helper)
    if isinstance(params, dict) and params.get("_merged"):
        params_dict = dict(params)
        params_dict.pop("_merged", None)
    else:
        params_dict = merge_vision_params(ctx, dict(params) if isinstance(params, dict) else None)

    def _export_on_main_thread() -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        # Graphic lookup, locale, and PNG export are UNO. OCR itself is not.
        local_params = dict(params_dict)
        image_name = local_params.get("image_name")
        graphic_obj = None
        if image_name:
            graphic_obj = _get_graphic_object(doc, str(image_name))
        else:
            graphic_obj = _first_selected_object(doc)

        if not local_params.get("lang"):
            local_params["lang"] = _resolve_locale_language(ctx, doc, graphic_obj)

        png_bytes = resolve_vision_image_bytes(
            ctx,
            doc,
            image_name=str(image_name) if image_name is not None else None,
        )
        source = "graphic_name" if str(image_name or "").strip() else "selection"
        context: dict[str, Any] = {"source": source}
        if source == "graphic_name":
            context["image_name"] = str(image_name).strip()
        return png_bytes, local_params, context

    png_bytes, params_out, context = execute_on_main_thread(_export_on_main_thread)
    if len(png_bytes) > VISION_IMAGE_MAX_BYTES:
        raise ToolExecutionError(
            _("Image is too large to send to the vision worker ({size} bytes; limit {limit}).").format(
                size=len(png_bytes), limit=VISION_IMAGE_MAX_BYTES
            ),
            code="IMAGE_TOO_LARGE",
            details={"size": len(png_bytes), "limit": VISION_IMAGE_MAX_BYTES},
        )
    spec: dict[str, Any] = {"helper": name, "params": params_out}
    stop_checker = getattr(ctx, "stop_checker", None)
    # venv OCR (up to the long worker budget, ~120s) stays on this thread.
    return run_vision(ctx, spec, png_bytes, context=context, stop_checker=stop_checker)


def run_and_insert_vision_for_selection(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any] | None = None, insert_into_document: bool = True, stop_checker: Any = None) -> dict[str, Any]:
    """OCR each graphic in the selection (or one named image) and optionally insert.

    Discovers named graphics while the selection is intact, then OCRs and inserts
    by ``image_name`` so a text-range selection is collapsed before any edit and
    intervening text is never replaced.

    UNO discovery and insert are marshaled to the main thread. The venv OCR call
    inside ``run_trusted_vision`` is not.
    """
    name = _validate_helper(helper)

    if stop_checker is None:
        stop_checker = getattr(ctx, "stop_checker", None)
    params_dict = merge_vision_params(ctx, dict(params) if isinstance(params, dict) else None)
    params_dict["_merged"] = True
    explicit_name = str(params_dict.get("image_name") or "").strip()

    def _discover_names() -> list[str]:
        if explicit_name:
            return [explicit_name]
        # Capture names before any insert collapses/clears the selection.
        from plugin.doc.visual_helpers import graphic_objects_in_selection

        pairs = graphic_objects_in_selection(doc)
        found = [n for n, _unused in pairs if n]
        if not found:
            raise ToolExecutionError(_("Select an embedded image (or a range containing images), then Run again."), code="NO_IMAGE_SELECTED")
        return found

    results: list[dict[str, Any]] = []
    target_names: list[str] = []
    stopped_early = False
    try:
        target_names = execute_on_main_thread(_discover_names)

        for image_name in target_names:
            if stop_checker is not None and stop_checker():
                if results:
                    # Prior images were already inserted into the document. Break loop
                    # and return completed results so the tool result matches what landed.
                    stopped_early = True
                    break
                return {"status": "error", "code": "USER_STOPPED", "message": _("Cancelled by user.")}

            per_params = dict(params_dict)
            per_params["image_name"] = image_name
            result = run_trusted_vision(ctx, doc, helper=name, params=per_params)

            if result.get("status") == "error":
                # Attach individual_results on failure so images that already
                # finished can still be inserted. The error dict's
                # images_processed / image_names do not carry those payloads.
                failed = dict(result)
                failed["images_processed"] = len(results)
                failed["image_names"] = list(target_names[: len(results)])
                failed["failed_image"] = image_name
                failed["inserted"] = bool(insert_into_document and results)
                failed["partial"] = bool(results)
                failed["individual_results"] = list(results)
                return failed
            # A finished OCR is a document mutation and is inserted. Stop is checked
            # at the top of the loop, so the next image is not started.
            if insert_into_document:
                # prepare_vision_writer_insert collapses any range selection before HTML import.
                def _insert(res: dict[str, Any] = result, per_insert: dict[str, Any] = per_params) -> None:
                    from plugin.vision.vision_egress import insert_vision_result

                    insert_vision_result(ctx, doc, res, params=per_insert)

                # Once the bytes are in hand, insert with bound_scope=None.
                # The default scope is the send cancellation, so Stop would
                # abort a document mutation that already has its data.
                execute_on_main_thread(_insert, bound_scope=None)
            result["image_name"] = image_name
            if "context" not in result or not isinstance(result["context"], dict):
                result["context"] = {"image_name": image_name}
            else:
                result["context"]["image_name"] = image_name
            results.append(result)
    except SendCancelled:
        err_out: dict[str, Any] = {"status": "error", "code": "USER_STOPPED", "message": _("Cancelled by user.")}
        if results:
            err_out["images_processed"] = len(results)
            err_out["image_names"] = list(target_names[: len(results)])
            err_out["inserted"] = bool(insert_into_document and results)
            err_out["partial"] = True
            err_out["individual_results"] = list(results)
        return err_out

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
    # A Stop break returns only the names that finished, with stopped=True,
    # partial=True, and "Stopped after N of M images". Reporting every
    # target_name as "OCR complete" hides the partial run.
    if stopped_early:
        image_names = list(target_names[: len(results)])
        message = _("Stopped after {count} of {total} images.").format(count=len(results), total=len(target_names))
    elif inserted:
        image_names = list(target_names)
        message = _("OCR complete ({count} images).").format(count=len(results)) if len(results) > 1 else _("OCR complete.")
    else:
        image_names = list(target_names)
        message = _("OCR complete (text returned only; not inserted).")

    res_out: dict[str, Any] = {
        "status": "ok",
        "helper": name,
        "full_text": "\n\n".join(part for part in full_parts if part),
        "html": results[-1].get("html") if len(results) == 1 else "",
        "metrics": metrics,
        "warnings": warnings,
        "inserted": inserted,
        "images_processed": len(results),
        "image_names": image_names,
        "image_name": image_names[0] if image_names else None,
        "message": message,
        "results": results if len(results) > 1 else None,
    }
    if stopped_early:
        res_out["stopped"] = True
        res_out["partial"] = True
    return res_out
