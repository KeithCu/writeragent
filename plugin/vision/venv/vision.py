# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv vision helpers — Docling (default) and PaddleOCR fallback.

Invoked from the LO host through a fixed RPC stub (see plugin.scripting.client.run_vision), not
from LLM-submitted code. See docs/images/recognition.md.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

from plugin.vision.vision_common import (
    DEFAULT_VISION_INSERT_MODE,
    IMPLEMENTED_HELPERS,
    VISION_IMAGE_MAX_BYTES,
    detect_vision_input_format,
    error_result,
    fallback_engine_enabled,
    resolve_engine,
)

log = logging.getLogger(__name__)

__all__ = ["apply_structured_insert_html", "run_vision"]


def _run_backend(module_name: str, helper: str, image: Any, params: dict[str, Any]) -> dict[str, Any]:
    mod = importlib.import_module(f"plugin.vision.venv.vision_{module_name}")
    if helper == "extract_text":
        return mod.extract_text(image, params)
    return mod.extract_structure(image, params)


def _apply_paddle_fallback(
    docling_result: dict[str, Any],
    helper: str,
    image: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    # PDF bytes stay on the Docling error: Paddle decode is PNG/JPEG only, and
    # decoding %PDF as an image only logs a second failure.
    # Fall back only for DOCLING_UNAVAILABLE (package missing) or
    # DOCLING_API_MISMATCH (Docling version break, issue 587). Docling emits
    # that code instead of matching brittle substrings ('get_engine_config' /
    # 'LayoutModelConfig'). OCR_BACKEND_UNAVAILABLE (user-configured rapidocr
    # missing) and LAYOUT_MODEL_UNAVAILABLE are configuration errors for the
    # requested engine and must not fall back.
    code = docling_result.get("code")
    if (
        docling_result.get("status") != "error"
        or code not in ("DOCLING_UNAVAILABLE", "DOCLING_API_MISMATCH")
        or not fallback_engine_enabled(params)
        or detect_vision_input_format(image, params) == "pdf"
    ):
        return docling_result

    try:
        paddle_result = _run_backend("paddle", helper, image, params)
    except Exception:
        log.exception("Paddle fallback helper %s failed", helper)
        return docling_result

    if paddle_result.get("status") != "ok":
        return docling_result

    msg = f"{code or 'Docling unavailable'}; fell back to PaddleOCR."
    paddle_result["warnings"] = [msg, *(paddle_result.get("warnings") or [])]
    paddle_result["metrics"] = {**dict(paddle_result.get("metrics") or {}), "fallback_from": "docling"}
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
        return _run_backend("paddle", helper, image, params)

    result = _run_backend("docling", helper, image, params)
    return _apply_paddle_fallback(result, helper, image, params)


def apply_structured_insert_html(result: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """When insert_mode=structured, replace html in the worker before LO insert (css-inline lives in venv)."""
    if result.get("status") != "ok" or str(params.get("insert_mode") or DEFAULT_VISION_INSERT_MODE).strip().lower() != "structured":
        return result
    from plugin.vision.venv.vision_html_export import structured_html_from_vision_result

    html = structured_html_from_vision_result(result)
    if not html.strip():
        return result
    updated = dict(result)
    # Calc structured insert falls back to HTML when the cell grid is empty.
    # Overwriting html used to make that fallback the bbox fragment, not Docling.
    updated.setdefault("html_docling", result.get("html"))
    updated["html"] = html
    return updated


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

    raw_params = spec_dict.get("params")
    if raw_params is not None and not isinstance(raw_params, dict):
        return error_result("INVALID_PARAMS", "spec.params must be a dict", helper=helper)
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    if isinstance(image, (bytes, bytearray)) and len(image) > VISION_IMAGE_MAX_BYTES:
        return error_result(
            "IMAGE_TOO_LARGE",
            f"Image is {len(image)} bytes; limit is {VISION_IMAGE_MAX_BYTES}.",
            helper=helper,
        )

    try:
        result = _dispatch_helper(helper, image, params)
        return apply_structured_insert_html(result, params)
    except Exception as exc:
        log.exception("Vision helper %s failed", helper)
        return error_result("VISION_ERROR", str(exc), helper=helper)
