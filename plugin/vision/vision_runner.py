# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted vision execution for Run Python Script (Writer graphic export + venv RPC)."""

from __future__ import annotations

from typing import Any

from dataclasses import dataclass, field

from plugin.framework.doc_type import is_calc, is_writer
from plugin.doc.visual_helpers import get_graphic_object_by_name as _get_graphic_object, selected_graphic_object
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed
from plugin.framework.i18n import _
from plugin.framework.queue_executor import main
from plugin.framework.send_cancellation import SendCancellation, SendCancelled, is_cancelled
from plugin.scripting.client import run_vision
from plugin.vision.vision_common import (
    HELPER_NAMES,
    VISION_IMAGE_MAX_BYTES,
    merge_vision_params,
)
from plugin.writer.images.image_tools import export_graphic_object_to_bytes


def supports_vision_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Vision Helpers for *doc*."""
    if doc is None:
        return False
    return is_writer(doc) or is_calc(doc)


def extract_vision_insert_kwargs(ctx: Any, code: str) -> dict[str, Any]:
    """Extract vision insertion kwargs (merged params) from script code."""
    from plugin.scripting.helper_domain import ScriptFacts

    call_spec = ScriptFacts.from_source(code).first_run_spec("run_vision") or {}
    raw_params = call_spec.get("params") if isinstance(call_spec.get("params"), dict) else None
    return {"params": merge_vision_params(ctx, raw_params)}


def get_selected_image_bytes(ctx: Any, doc: Any) -> bytes:
    """Export the currently selected embedded graphic as raw PNG bytes."""
    try:
        graphic_obj = selected_graphic_object(doc)
        png_bytes = export_graphic_object_to_bytes(ctx, graphic_obj) if graphic_obj is not None else None
    except Exception as exc:
        raise ToolExecutionError(
            _("Could not export the selected image: {error}").format(error=exc),
            code="IMAGE_EXPORT_FAILED",
        ) from exc
    if not png_bytes:
        raise ToolExecutionError(_("Select an embedded image (or a range containing images), then Run again."), code="NO_IMAGE_SELECTED")
    return png_bytes


def resolve_vision_image_bytes(ctx: Any, doc: Any, *, image_name: str | None = None, graphic_obj: Any = None) -> bytes:
    """Export PNG bytes from *image_name* or the current graphic selection."""
    name = str(image_name or "").strip()
    if not name and graphic_obj is None:
        return get_selected_image_bytes(ctx, doc)

    if graphic_obj is None:
        graphic_obj = _get_graphic_object(doc, name)
    if graphic_obj is None:
        raise ToolExecutionError(_("Image '{name}' not found. Use image_list or leave image_name empty and select the graphic.").format(name=name), code="IMAGE_NOT_FOUND", details={"image_name": name})
    try:
        png_bytes = export_graphic_object_to_bytes(ctx, graphic_obj)
    except Exception as exc:
        raise ToolExecutionError(
            _("Image '{name}' could not be exported: {error}").format(name=name, error=exc),
            code="IMAGE_EXPORT_FAILED",
            details={"image_name": name},
        ) from exc
    if not png_bytes:
        raise ToolExecutionError(_("Image '{name}' could not be exported.").format(name=name), code="IMAGE_NOT_FOUND", details={"image_name": name})
    return png_bytes


def _validate_helper(helper: str) -> str:
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="VISION_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="VISION_ERROR")
    return name


def _resolve_locale_language(ctx: Any, graphic_obj: Any) -> str:
    # 1. CharLocale from the target graphic
    if graphic_obj is not None:
        try:
            locale = graphic_obj.getPropertyValue("CharLocale")
            if locale and getattr(locale, "Language", None):
                return str(locale.Language).lower()
        except Exception as exc:
            reraise_if_disposed(exc)

    # 2. LibreOffice UI locale
    from plugin.framework.i18n import get_lo_locale

    lo_locale = get_lo_locale(ctx)
    if lo_locale:
        return lo_locale.split("_")[0].split("-")[0].lower()

    return "en"


def run_trusted_vision(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any] | None = None, cancel: SendCancellation | None = None) -> dict[str, Any]:
    """Export graphic bytes and run a trusted vision helper in the user venv.

    *cancel* aborts the venv OCR mid-run. Callers pass it explicitly: *ctx* is
    the UNO component context, which never carries the tool's stop flag.
    """
    name = _validate_helper(helper)
    params_dict = merge_vision_params(ctx, dict(params) if params is not None else None)

    def _export_on_main_thread() -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        # Graphic lookup, locale, and PNG export are UNO. OCR itself is not.
        local_params = dict(params_dict)
        image_name = local_params.get("image_name")
        name_str = str(image_name).strip() if image_name is not None else ""
        graphic_obj = _get_graphic_object(doc, name_str) if name_str else selected_graphic_object(doc)

        if not local_params.get("lang"):
            local_params["lang"] = _resolve_locale_language(ctx, graphic_obj)

        png_bytes = resolve_vision_image_bytes(ctx, doc, image_name=name_str or None, graphic_obj=graphic_obj)
        context: dict[str, Any] = {"source": "graphic_name", "image_name": name_str} if name_str else {"source": "selection"}
        return png_bytes, local_params, context

    png_bytes, params_out, context = main.call(_export_on_main_thread)
    if len(png_bytes) > VISION_IMAGE_MAX_BYTES:
        raise ToolExecutionError(
            _("Image is too large to send to the vision worker ({size} bytes; limit {limit}).").format(
                size=len(png_bytes), limit=VISION_IMAGE_MAX_BYTES
            ),
            code="IMAGE_TOO_LARGE",
            details={"size": len(png_bytes), "limit": VISION_IMAGE_MAX_BYTES},
        )
    spec: dict[str, Any] = {"helper": name, "params": {k: v for k, v in params_out.items() if k != "image_name"}}
    # venv OCR (up to the long worker budget, ~120s) stays on this thread.
    return run_vision(ctx, spec, png_bytes, context=context, cancel=cancel)


@dataclass
class VisionBatch:
    """Accumulates and formats batch multi-image OCR results."""

    helper: str
    target_names: list[str] = field(default_factory=list)
    insert_into_document: bool = True
    results: list[dict[str, Any]] = field(default_factory=list)
    failures: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    stopped_early: bool = False
    cancelled: bool = False

    def payload(self) -> dict[str, Any]:
        if self.cancelled:
            err_out: dict[str, Any] = {"status": "error", "code": "USER_STOPPED", "message": _("Cancelled by user.")}
            if self.results:
                err_out["images_processed"] = len(self.results)
                err_out["image_names"] = list(self.target_names[: len(self.results)])
                err_out["inserted"] = bool(self.insert_into_document and self.results)
                err_out["partial"] = True
                err_out["individual_results"] = list(self.results)
            return err_out

        if self.failures:
            first_name, first = self.failures[0]
            failed = dict(first)
            failed_names = [n for n, _unused in self.failures]
            failed["images_processed"] = len(self.results)
            failed["image_names"] = [str(r.get("image_name")) for r in self.results]
            failed["failed_image"] = first_name
            failed["failed_images"] = failed_names
            failed["inserted"] = bool(self.insert_into_document and self.results)
            failed["partial"] = bool(self.results)
            failed["individual_results"] = list(self.results)
            if len(self.target_names) > 1:
                failed["message"] = _("OCR failed for {failed} ({error}); {count} of {total} images completed.").format(
                    failed=", ".join(failed_names),
                    error=str(first.get("message") or "error"),
                    count=len(self.results),
                    total=len(self.target_names),
                )
            return failed

        full_parts = [str(r.get("full_text") or "") for r in self.results]
        warnings: list[Any] = []
        for r in self.results:
            w = r.get("warnings")
            if isinstance(w, list):
                warnings.extend(w)
        metrics: dict[str, Any] = {"images_processed": len(self.results)}
        if len(self.results) == 1:
            single_metrics = self.results[0].get("metrics")
            if isinstance(single_metrics, dict):
                metrics.update(single_metrics)

        inserted = bool(self.insert_into_document)
        if self.stopped_early:
            image_names = list(self.target_names[: len(self.results)])
            message = _("Stopped after {count} of {total} images.").format(count=len(self.results), total=len(self.target_names))
        elif inserted:
            image_names = list(self.target_names)
            message = _("OCR complete ({count} images).").format(count=len(self.results)) if len(self.results) > 1 else _("OCR complete.")
        else:
            image_names = list(self.target_names)
            message = _("OCR complete (text returned only; not inserted).")

        res_out: dict[str, Any] = {
            "status": "ok",
            "helper": self.helper,
            "full_text": "\n\n".join(part for part in full_parts if part),
            "html": self.results[-1].get("html") if len(self.results) == 1 else "",
            "metrics": metrics,
            "warnings": warnings,
            "inserted": inserted,
            "images_processed": len(self.results),
            "image_names": image_names,
            "image_name": image_names[0] if image_names else None,
            "message": message,
            "individual_results": list(self.results),
        }
        if self.stopped_early:
            res_out["stopped"] = True
            res_out["partial"] = True
        return res_out


def run_and_insert_vision_for_selection(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any] | None = None, insert_into_document: bool = True, cancel: SendCancellation | None = None) -> dict[str, Any]:
    """OCR each graphic in the selection (or one named image) and optionally insert.

    Discovers named graphics while the selection is intact, then OCRs and inserts
    by ``image_name`` so a text-range selection is collapsed before any edit and
    intervening text is never replaced.

    UNO discovery and insert are marshaled to the main thread. The venv OCR call
    inside ``run_trusted_vision`` is not.
    """
    name = _validate_helper(helper)

    params_dict = dict(params) if params is not None else {}
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

    batch = VisionBatch(helper=name, insert_into_document=insert_into_document)
    try:
        batch.target_names = main.call(_discover_names)

        for image_name in batch.target_names:
            if is_cancelled(cancel):
                if batch.results:
                    batch.stopped_early = True
                    break
                batch.cancelled = True
                return batch.payload()

            per_params = dict(params_dict)
            per_params["image_name"] = image_name
            result = run_trusted_vision(ctx, doc, helper=name, params=per_params, cancel=cancel)

            if result.get("status") == "error":
                if result.get("code") == "USER_STOPPED":
                    if not batch.results:
                        return result
                    batch.stopped_early = True
                    break
                batch.failures.append((image_name, result))
                continue

            if insert_into_document:
                def _insert(res: dict[str, Any] = result, per_insert: dict[str, Any] = per_params) -> None:
                    from plugin.vision.vision_egress import insert_vision_result

                    insert_vision_result(ctx, doc, res, params=per_insert)

                main.call(_insert, bound_scope=None)
            result["image_name"] = image_name
            if "context" not in result or not isinstance(result["context"], dict):
                result["context"] = {"image_name": image_name}
            else:
                result["context"]["image_name"] = image_name
            batch.results.append(result)

        return batch.payload()
    except SendCancelled:
        batch.cancelled = True
        return batch.payload()
