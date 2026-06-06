# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv vision helpers — local OCR and detection via PaddleOCR / Ultralytics.

Invoked from the LO host through a fixed RPC stub (see vision_client.py), not
from LLM-submitted code. See docs/image-recognition.md.
"""
from __future__ import annotations

import io
import importlib
import logging
from typing import Any

log = logging.getLogger(__name__)

HELPER_NAMES = frozenset(
    {
        "extract_text",
        "extract_structure",
        "detect_objects",
        "detect_layout",
        "recognize_pipeline",
        "perceptual_hash",
    }
)

_IMPLEMENTED_HELPERS = frozenset({"extract_text"})

_paddle_ocr_engine: Any = None
_paddle_ocr_lang: str | None = None


def _ok_result(helper: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "helper": helper, **payload}


def _error_result(code: str, message: str, *, helper: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    if details:
        out["details"] = details
    return out


def _box_to_xywh(box_points: Any) -> list[int]:
    """Convert PaddleOCR quadrilateral corners to [x, y, w, h] in PNG pixel space."""
    xs: list[float] = []
    ys: list[float] = []
    for point in box_points:
        xs.append(float(point[0]))
        ys.append(float(point[1]))
    if not xs or not ys:
        return [0, 0, 0, 0]
    x_min = int(min(xs))
    y_min = int(min(ys))
    x_max = int(max(xs))
    y_max = int(max(ys))
    return [x_min, y_min, max(0, x_max - x_min), max(0, y_max - y_min)]


def _decode_image_bytes(image: Any) -> Any:
    """Return a numpy RGB array from raw PNG/JPEG bytes."""
    if image is None:
        raise ValueError("image bytes are required")
    if not isinstance(image, (bytes, bytearray)):
        raise ValueError("image must be raw bytes")
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError("Pillow is required to decode image bytes for OCR") from exc
    import numpy as np

    with Image.open(io.BytesIO(bytes(image))) as img:
        rgb = img.convert("RGB")
        return np.array(rgb)


def _get_paddle_ocr(lang: str) -> Any:
    """Lazy-init one PaddleOCR instance per worker process (module singleton)."""
    global _paddle_ocr_engine, _paddle_ocr_lang
    if _paddle_ocr_engine is not None and _paddle_ocr_lang == lang:
        return _paddle_ocr_engine
    try:
        paddleocr_mod = importlib.import_module("paddleocr")
        paddle_ocr_cls = paddleocr_mod.PaddleOCR
    except ImportError as exc:
        raise ImportError("paddleocr is not installed") from exc
    _paddle_ocr_engine = paddle_ocr_cls(use_angle_cls=True, lang=lang, show_log=False)
    _paddle_ocr_lang = lang
    return _paddle_ocr_engine


def _run_paddle_ocr(engine: Any, image_array: Any) -> list[Any]:
    """Call PaddleOCR across 2.x/3.x API differences."""
    if hasattr(engine, "ocr"):
        result = engine.ocr(image_array, cls=True)
    elif hasattr(engine, "predict"):
        result = engine.predict(image_array)
    else:
        raise RuntimeError("PaddleOCR engine has no ocr or predict method")
    if not result:
        return []
    page = result[0] if isinstance(result, list) else result
    if not page:
        return []
    return list(page) if isinstance(page, list) else []


def _parse_ocr_lines(raw_lines: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    regions: list[dict[str, Any]] = []
    texts: list[str] = []
    for line in raw_lines:
        if not line or not isinstance(line, (list, tuple)) or len(line) < 2:
            continue
        box_raw, text_info = line[0], line[1]
        if isinstance(text_info, (list, tuple)) and text_info:
            text = str(text_info[0] or "").strip()
            confidence = float(text_info[1]) if len(text_info) > 1 else 0.0
        elif isinstance(text_info, str):
            text = text_info.strip()
            confidence = 0.0
        else:
            continue
        if not text:
            continue
        regions.append(
            {
                "box": _box_to_xywh(box_raw),
                "text": text,
                "confidence": confidence,
            }
        )
        texts.append(text)
    return regions, texts


def _extract_text(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_text"
    lang = str(params.get("lang") or "en").strip() or "en"
    try:
        engine = _get_paddle_ocr(lang)
    except ImportError:
        return _error_result(
            "PADDLEOCR_UNAVAILABLE",
            "Install paddleocr and paddlepaddle in your venv (Settings → Python).",
            helper=helper,
        )

    try:
        image_array = _decode_image_bytes(image)
        raw_lines = _run_paddle_ocr(engine, image_array)
        regions, texts = _parse_ocr_lines(raw_lines)
    except Exception as exc:
        log.exception("extract_text OCR failed")
        return _error_result("VISION_ERROR", str(exc), helper=helper)

    full_text = "\n".join(texts)
    warnings: list[str] = []
    if not full_text:
        warnings.append("No text detected.")

    confidences = [float(r["confidence"]) for r in regions if r.get("confidence") is not None]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    line_count = len(texts) if texts else (0 if not full_text else len(full_text.splitlines()))

    return _ok_result(
        helper,
        full_text=full_text,
        regions=regions,
        metrics={"line_count": line_count, "mean_confidence": mean_confidence},
        warnings=warnings,
    )


def _dispatch_helper(helper: str, image: Any, params: dict[str, Any]) -> dict[str, Any]:
    if helper not in _IMPLEMENTED_HELPERS:
        return _error_result(
            "UNKNOWN_HELPER",
            f"Helper {helper!r} is not implemented yet.",
            helper=helper,
        )
    if helper == "extract_text":
        return _extract_text(image, params)
    return _error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)


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
        return _dispatch_helper(helper, image, params)
    except Exception as exc:
        log.exception("Vision helper %s failed", helper)
        return _error_result("VISION_ERROR", str(exc), helper=helper)
