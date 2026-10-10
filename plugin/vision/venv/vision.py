# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv vision helpers — Docling (default) and PaddleOCR fallback.

Invoked from the LO host through a fixed RPC stub (see plugin.scripting.client.run_vision), not
from LLM-submitted code. See docs/images/recognition.md.
"""
from __future__ import annotations

import logging
from typing import Any

from plugin.vision.vision_common import (
    HELPER_NAMES,
    IMPLEMENTED_HELPERS,
    VISION_IMAGE_MAX_BYTES,
    error_result,
    fallback_engine_enabled,
    resolve_engine,
)

log = logging.getLogger(__name__)

__all__ = ["run_vision"]


def _run_paddle_helper(helper: str, image: Any, params: dict[str, Any]) -> dict[str, Any]:
    from plugin.vision.venv import vision_paddle as paddle_backend

    if helper == "extract_text":
        return paddle_backend.extract_text(image, params)
    return paddle_backend.extract_structure(image, params)


def _run_docling_helper(helper: str, image: Any, params: dict[str, Any]) -> dict[str, Any]:
    from plugin.vision.venv import vision_docling as docling_backend

    if helper == "extract_text":
        return docling_backend.extract_text(image, params)
    return docling_backend.extract_structure(image, params)


def _is_docling_fallback_candidate(code: str | None) -> bool:
    """True for errors where falling back to PaddleOCR makes sense.

    DOCLING_UNAVAILABLE: Docling package is missing/cannot import.
    DOCLING_API_MISMATCH: Docling internals broke due to version mismatch (issue 587).
    Note: OCR_BACKEND_UNAVAILABLE (e.g. user-configured rapidocr missing) and LAYOUT_MODEL_UNAVAILABLE
    represent configuration issues for the requested engine/options and do not trigger fallback.
    """
    return code in ("DOCLING_UNAVAILABLE", "DOCLING_API_MISMATCH")


def _apply_paddle_fallback(
    docling_result: dict[str, Any],
    helper: str,
    image: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    if docling_result.get("status") != "error":
        return docling_result
    from plugin.vision.vision_common import detect_vision_input_format

    # Paddle decode is PNG/JPEG only. A PDF that Docling rejected must keep that
    # error; decoding %PDF as an image only logs a second failure.
    if detect_vision_input_format(image, params) == "pdf":
        return docling_result
    code = docling_result.get("code")
    # Docling emits DOCLING_API_MISMATCH instead of matching brittle substrings
    # ('get_engine_config' / 'LayoutModelConfig'). Paddle fallback exceptions
    # are caught and logged so the Docling error in docling_result is kept.
    if not _is_docling_fallback_candidate(code):
        return docling_result
    if not fallback_engine_enabled(params):
        return docling_result

    try:
        paddle_result = _run_paddle_helper(helper, image, params)
    except Exception:
        log.exception("Paddle fallback helper %s failed", helper)
        return docling_result

    if paddle_result.get("status") != "ok":
        return docling_result

    warnings = list(paddle_result.get("warnings") or [])
    if code == "DOCLING_API_MISMATCH":
        warnings.insert(0, "Docling layout API error; fell back to PaddleOCR.")
    else:
        warnings.insert(0, "Docling unavailable; fell back to PaddleOCR.")
    paddle_result["warnings"] = warnings
    metrics = dict(paddle_result.get("metrics") or {})
    metrics["fallback_from"] = "docling"
    paddle_result["metrics"] = metrics
    return paddle_result


def _dispatch_helper(helper: str, image: Any, params: dict[str, Any]) -> dict[str, Any]:
    if helper not in IMPLEMENTED_HELPERS:
        return error_result(
            "UNKNOWN_HELPER",
            f"Helper {helper!r} is not implemented yet.",
            helper=helper,
        )

    try:
        engine = resolve_engine(params)
    except ValueError as exc:
        return error_result("INVALID_PARAMS", str(exc), helper=helper)

    if engine == "paddle":
        return _run_paddle_helper(helper, image, params)

    result = _run_docling_helper(helper, image, params)
    return _apply_paddle_fallback(result, helper, image, params)


def run_vision(
    spec: dict[str, Any] | str,
    image: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC and future tools."""
    del context  # reserved for future helpers (source, graphic name, etc.)
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return error_result("INVALID_SPEC", "spec must be a dict or helper name string")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return error_result("MISSING_HELPER", "spec.helper is required")
    if helper not in HELPER_NAMES:
        return error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params: dict[str, Any] = spec_dict["params"] if isinstance(spec_dict.get("params"), dict) else {}

    if isinstance(image, (bytes, bytearray)) and len(image) > VISION_IMAGE_MAX_BYTES:
        return error_result(
            "IMAGE_TOO_LARGE",
            f"Image is {len(image)} bytes; limit is {VISION_IMAGE_MAX_BYTES}.",
            helper=helper,
        )

    try:
        result = _dispatch_helper(helper, image, params)
        from plugin.vision.venv.vision_html_export import apply_structured_insert_html

        return apply_structured_insert_html(result, params)
    except Exception as exc:
        log.exception("Vision helper %s failed", helper)
        return error_result("VISION_ERROR", str(exc), helper=helper)
