# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv vision helpers — Docling (default) and PaddleOCR fallback.

Invoked from the LO host through a fixed RPC stub (see vision_client.py), not
from LLM-submitted code. See docs/image-recognition.md.
"""
from __future__ import annotations

import logging
from typing import Any

from plugin.scripting.vision_common import (
    HELPER_NAMES,
    IMPLEMENTED_HELPERS,
    _error_result,
    fallback_engine_enabled,
    resolve_engine,
)

log = logging.getLogger(__name__)

# Re-export for templates and tests.
__all__ = ["HELPER_NAMES", "run_vision"]


def _run_paddle_helper(helper: str, image: Any, params: dict[str, Any]) -> dict[str, Any]:
    from plugin.scripting import vision_paddle as paddle_backend

    if helper == "extract_text":
        return paddle_backend.extract_text(image, params)
    return paddle_backend.extract_structure(image, params)


def _run_docling_helper(helper: str, image: Any, params: dict[str, Any]) -> dict[str, Any]:
    from plugin.scripting import vision_docling as docling_backend

    if helper == "extract_text":
        return docling_backend.extract_text(image, params)
    return docling_backend.extract_structure(image, params)


def _apply_paddle_fallback(
    docling_result: dict[str, Any],
    helper: str,
    image: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    if docling_result.get("status") != "error":
        return docling_result
    if docling_result.get("code") != "DOCLING_UNAVAILABLE":
        return docling_result
    if not fallback_engine_enabled(params):
        return docling_result

    paddle_result = _run_paddle_helper(helper, image, params)
    if paddle_result.get("status") != "ok":
        return docling_result

    warnings = list(paddle_result.get("warnings") or [])
    warnings.insert(0, "Docling unavailable; fell back to PaddleOCR.")
    paddle_result["warnings"] = warnings
    metrics = dict(paddle_result.get("metrics") or {})
    metrics["fallback_from"] = "docling"
    paddle_result["metrics"] = metrics
    return paddle_result


def _dispatch_helper(helper: str, image: Any, params: dict[str, Any]) -> dict[str, Any]:
    if helper not in IMPLEMENTED_HELPERS:
        return _error_result(
            "UNKNOWN_HELPER",
            f"Helper {helper!r} is not implemented yet.",
            helper=helper,
        )

    engine = resolve_engine(params)
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
        return _error_result("INVALID_SPEC", "spec must be a dict or helper name string")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return _error_result("MISSING_HELPER", "spec.helper is required")
    if helper not in HELPER_NAMES:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params: dict[str, Any] = spec_dict["params"] if isinstance(spec_dict.get("params"), dict) else {}

    try:
        result = _dispatch_helper(helper, image, params)
        from plugin.scripting.vision_html_export import apply_structured_insert_html

        return apply_structured_insert_html(result, params)
    except Exception as exc:
        log.exception("Vision helper %s failed", helper)
        return _error_result("VISION_ERROR", str(exc), helper=helper)
