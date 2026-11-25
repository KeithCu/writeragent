# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted vision execution for Run Python Script (Writer graphic export + venv RPC)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from dataclasses import dataclass, field

from plugin.framework.doc_type import is_calc, is_draw, is_writer
from plugin.doc import visual_helpers
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from plugin.framework.i18n import _
from plugin.framework.queue_executor import main
from plugin.framework.send_cancellation import SendCancellation, SendCancelled, is_cancelled
from plugin.scripting.client import run_vision
from plugin.vision.vision_common import (
    HELPER_NAMES,
    VISION_IMAGE_MAX_BYTES,
    error_result,
    merge_vision_params,
)
from plugin.writer.images.image_tools import export_graphic_object_to_bytes

if TYPE_CHECKING:
    from plugin.vision.vision_egress import VisionInsertOutcome

log = logging.getLogger(__name__)


def supports_vision_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Vision Helpers for *doc*."""
    if doc is None:
        return False
    return is_writer(doc) or is_calc(doc) or is_draw(doc)


def extract_vision_insert_kwargs(ctx: Any, code: str) -> dict[str, Any]:
    """Extract vision insertion kwargs (merged params) from script code."""
    from plugin.scripting.helper_domain import ScriptFacts

    call_spec = ScriptFacts.from_source(code).first_run_spec("run_vision") or {}
    raw_params = call_spec.get("params") if isinstance(call_spec.get("params"), dict) else None
    return {"params": merge_vision_params(ctx, raw_params)}


def discover_vision_images(doc: Any) -> list[tuple[str, Any | None]]:
    """Return ``(name, graphic_or_none)`` pairs for the current selection.

    Writer images are looked up by name because Writer insert resolves and
    presence-checks by name (``insert_vision_result_into_writer``); in other
    documents a name may be empty or repeat on another sheet, so the shape is
    kept.
    """
    pairs = visual_helpers.graphic_objects_in_selection(doc)
    if not pairs:
        raise visual_helpers.no_image_selected_error(doc)
    if is_writer(doc):
        return [(name, None) for name, _unused in pairs]
    return pairs


def resolve_vision_image(ctx: Any, doc: Any, image_name: str, graphic: Any | None = None) -> tuple[Any, bytes]:
    """Return the graphic to OCR and its PNG bytes.

    Uses *graphic* when given, else looks up *image_name*, else reads the selected graphic.
    """
    graphic_obj = visual_helpers.find_image(doc, image_name, graphic)
    try:
        png_bytes = export_graphic_object_to_bytes(ctx, graphic_obj)
    except Exception as exc:
        reraise_if_disposed(exc)
        if image_name:
            message = _("Image '{name}' could not be exported: {error}").format(name=image_name, error=exc)
        else:
            message = _("Could not export the selected image: {error}").format(error=exc)
        raise ToolExecutionError(message, code="IMAGE_EXPORT_FAILED", details={"image_name": image_name}) from exc
    if not png_bytes:
        # A kept shape of a closed document exports as no data; this read raises DocumentDisposedError.
        uno_read(doc.getURL, "")
        if not image_name:
            raise visual_helpers.no_image_selected_error(doc)
        raise ToolExecutionError(
            _("Image '{name}' could not be exported.").format(name=image_name),
            code="IMAGE_NOT_FOUND",
            details={"image_name": image_name},
        )
    return graphic_obj, png_bytes


def _validate_helper(helper: str) -> str:
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="VISION_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="VISION_ERROR")
    return name


def _resolve_locale_language(ctx: Any, graphic_obj: Any) -> str:
    # Keep the region so zh-TW reaches Docling and Paddle as Traditional Chinese.
    # 1. CharLocale from the target graphic
    try:
        locale = graphic_obj.getPropertyValue("CharLocale")
        if locale and locale.Language:
            return f"{locale.Language}-{locale.Country}" if locale.Country else locale.Language
    except Exception as exc:
        reraise_if_disposed(exc)

    # 2. LibreOffice UI locale
    from plugin.framework.i18n import get_lo_locale

    lo_locale = get_lo_locale(ctx)
    if lo_locale:
        return lo_locale.replace("_", "-")

    return "en"


def run_trusted_vision(
    ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    cancel: SendCancellation | None = None,
    on_exported: Callable[[dict[str, Any], bytes], None] | None = None,
    graphic: Any | None = None,
) -> dict[str, Any]:
    """Export graphic bytes and run a trusted vision helper in the user venv.

    *cancel* aborts the venv OCR mid-run. Callers pass it explicitly: *ctx* is
    the UNO component context, which never carries the tool's stop flag.
    *on_exported*, if given, runs on the calling thread after the export and
    size check with the exact params dict the OCR call sends and the PNG bytes.
    *graphic* is the shape discovery found for a non-Writer image.
    """
    name = _validate_helper(helper)
    params_dict = merge_vision_params(ctx, dict(params) if params is not None else None)

    def _export_on_main_thread() -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        # Graphic lookup, PNG export, and locale are UNO. OCR itself is not.
        local_params = dict(params_dict)
        image_name = str(local_params.get("image_name") or "").strip()
        graphic_obj, png_bytes = resolve_vision_image(ctx, doc, image_name, graphic)
        if not local_params.get("lang"):
            local_params["lang"] = _resolve_locale_language(ctx, graphic_obj)
        context: dict[str, Any] = {"source": "graphic_name", "image_name": image_name} if image_name else {"source": "selection"}
        return png_bytes, local_params, context

    try:
        png_bytes, params_out, context = main.call(_export_on_main_thread)
    except Exception as exc:
        # list_graphic_objects raises LibreOffice's own DisposedException for a closed Writer
        # document (sw/source/uibase/uno/unotxdoc.cxx:SwXTextDocument::ThrowIfInvalid).
        reraise_if_disposed(exc)
        raise
    if len(png_bytes) > VISION_IMAGE_MAX_BYTES:
        raise ToolExecutionError(
            _("Image is too large to send to the vision worker ({size} bytes; limit {limit}).").format(
                size=len(png_bytes), limit=VISION_IMAGE_MAX_BYTES
            ),
            code="IMAGE_TOO_LARGE",
            details={"size": len(png_bytes), "limit": VISION_IMAGE_MAX_BYTES},
        )
    spec: dict[str, Any] = {"helper": name, "params": {k: v for k, v in params_out.items() if k != "image_name"}}
    if on_exported is not None:
        on_exported(spec["params"], png_bytes)
    # venv OCR (can take minutes; client._resolve_vision_timeout_sec sets the limit) stays on this thread.
    return run_vision(ctx, spec, png_bytes, context=context, cancel=cancel)


@dataclass
class VisionBatch:
    """Accumulates and formats batch multi-image OCR results."""

    helper: str
    target_names: list[str] = field(default_factory=list)
    insert_into_document: bool = True
    results: list[dict[str, Any]] = field(default_factory=list)
    failures: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    deleted_images: list[str] = field(default_factory=list)
    kept_images: list[str] = field(default_factory=list)
    inserted_count: int = 0
    stopped_early: bool = False
    cancelled: bool = False

    def _source_image_fields(self) -> dict[str, Any]:
        """Names of images removed after insert, and of images kept because the delete cannot be tracked."""
        fields: dict[str, Any] = {}
        if self.deleted_images:
            fields["deleted_images"] = list(self.deleted_images)
        if self.kept_images:
            fields["kept_images_delete_not_trackable"] = list(self.kept_images)
        return fields

    def payload(self) -> dict[str, Any]:
        if self.cancelled:
            err_out: dict[str, Any] = {"status": "error", "code": "USER_STOPPED", "message": _("Cancelled by user.")}
            if self.results:
                err_out["images_processed"] = len(self.results)
                err_out["image_names"] = [str(r.get("image_name")) for r in self.results]
                err_out["inserted"] = self.inserted_count > 0
                err_out["partial"] = True
                err_out["individual_results"] = list(self.results)
                err_out.update(self._source_image_fields())
            return err_out

        if self.failures:
            first_name, first = self.failures[0]
            failed = dict(first)
            failed_names = [n for n, _unused in self.failures]
            failed["images_processed"] = len(self.results)
            failed["image_names"] = [str(r.get("image_name")) for r in self.results]
            failed["failed_image"] = first_name
            failed["failed_images"] = failed_names
            failed["inserted"] = self.inserted_count > 0
            failed["partial"] = bool(self.results)
            failed["individual_results"] = list(self.results)
            failed.update(self._source_image_fields())
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

        inserted = self.inserted_count > 0
        if self.stopped_early:
            image_names = list(self.target_names[: len(self.results)])
            message = _("Stopped after {count} of {total} images.").format(count=len(self.results), total=len(self.target_names))
        elif inserted:
            image_names = list(self.target_names)
            message = _("OCR complete ({count} images).").format(count=len(self.results)) if len(self.results) > 1 else _("OCR complete.")
        elif self.insert_into_document:
            image_names = list(self.target_names)
            message = _("OCR complete. No text detected.")
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
        res_out.update(self._source_image_fields())
        return res_out


def run_and_insert_vision_for_selection(
    ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    insert_into_document: bool = True,
    cancel: SendCancellation | None = None,
    on_exported: Callable[[dict[str, Any], bytes], None] | None = None,
    on_discovered: Callable[[list[tuple[str, Any | None]]], None] | None = None,
) -> dict[str, Any]:
    """OCR each graphic in the selection (or one named image) and optionally insert.

    Discovers graphics while the selection is intact, then OCRs and inserts by
    ``image_name`` (or the kept discovered shape for a Calc/Draw/Impress
    image) so a text-range selection is collapsed before any edit and intervening
    text is never replaced.

    *on_discovered*, if given, runs on the calling thread with the discovered
    ``(name, graphic_or_none)`` list and must not touch UNO.
    UNO discovery and insert are marshaled to the main thread. The venv OCR call
    inside ``run_trusted_vision`` is not.
    """
    name = _validate_helper(helper)

    params_dict = dict(params) if params is not None else {}
    explicit_name = str(params_dict.get("image_name") or "").strip()

    def _discover() -> list[tuple[str, Any | None]]:
        if explicit_name:
            return [(explicit_name, None)]
        # Capture names (and shapes for non-Writer images) before any insert collapses/clears the selection.
        return discover_vision_images(doc)

    batch = VisionBatch(helper=name, insert_into_document=insert_into_document)
    try:
        discovered = main.call(_discover)
        batch.target_names = [img_name for img_name, _unused in discovered]
        if on_discovered is not None:
            on_discovered(discovered)

        def _insert_one(res: dict[str, Any], per_insert: dict[str, Any], graphic: Any | None) -> VisionInsertOutcome:
            from plugin.vision.vision_egress import insert_vision_result

            return insert_vision_result(ctx, doc, res, params=per_insert, graphic=graphic)

        for idx, (image_name, graphic) in enumerate(discovered):
            if is_cancelled(cancel):
                if batch.results:
                    batch.stopped_early = True
                    break
                batch.cancelled = True
                return batch.payload()

            per_params = dict(params_dict)
            per_params["image_name"] = image_name
            outcome: VisionInsertOutcome | None = None
            try:
                result = run_trusted_vision(ctx, doc, helper=name, params=per_params, cancel=cancel, on_exported=on_exported, graphic=graphic)
                if result.get("status") != "error" and insert_into_document:
                    outcome = main.call(lambda: _insert_one(result, per_params, graphic), bound_scope=None)
            except SendCancelled:
                raise
            except ToolExecutionError as exc:
                if "worker" in exc.details:
                    if is_cancelled(cancel):
                        if batch.results:
                            batch.stopped_early = True
                            break
                        batch.cancelled = True
                        return batch.payload()
                    if not batch.results:
                        raise
                    log.exception("Vision OCR worker failed for image %r", image_name)
                    # A worker-level failure (timeout, IPC/spawn error) stops the run because the
                    # next image would likely hit it too, while finished images are still reported.
                    batch.failures.append((image_name, error_result(exc.code, str(exc), helper=name, details=exc.details)))
                    break
                batch.failures.append((image_name, error_result(exc.code, str(exc), helper=name, details=exc.details)))
                continue
            except Exception as exc:
                reraise_if_disposed(exc)
                if not batch.results:
                    raise
                log.exception("Vision OCR failed for image %r", image_name)
                # An unexpected exception stops the run because the next image would likely hit it too,
                # while finished images are still reported.
                batch.failures.append((image_name, error_result("VISION_ERROR", str(exc), helper=name)))
                break

            if result.get("status") == "error":
                if result.get("code") == "USER_STOPPED":
                    if not batch.results:
                        return result
                    batch.stopped_early = True
                    break
                batch.failures.append((image_name, result))
                continue

            if insert_into_document and outcome is not None:
                if outcome != "empty":
                    batch.inserted_count += 1
                if outcome == "image_deleted":
                    batch.deleted_images.append(image_name)
                elif outcome == "image_kept":
                    batch.kept_images.append(image_name)
            result["image_name"] = image_name
            result["image_index"] = idx
            if "context" not in result or not isinstance(result["context"], dict):
                result["context"] = {"image_name": image_name}
            else:
                result["context"]["image_name"] = image_name
            batch.results.append(result)

        return batch.payload()
    except SendCancelled:
        batch.cancelled = True
        return batch.payload()
