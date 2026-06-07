# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers for trusted vision backends (Paddle, Docling)."""
from __future__ import annotations

import io
from typing import Any

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

IMPLEMENTED_HELPERS = frozenset({"extract_text", "extract_structure"})

DEFAULT_ENGINE = "docling"
DEFAULT_OCR_BACKEND = "rapidocr"

MAX_TABLE_ROWS = 50

# Config keys merged from Settings → vision.* before template param overrides.
VISION_CONFIG_KEYS = (
    "device",
    "images_scale",
    "text_score",
    "force_full_page_ocr",
    "lang",
    "num_threads",
    "table_mode",
    "do_cell_matching",
    "create_orphan_clusters",
    "layout_model",
    "do_formula_enrichment",
    "do_code_enrichment",
    "document_timeout",
    "allow_external_plugins",
    "artifacts_path",
)


def merge_vision_params(ctx: Any, template_params: dict[str, Any] | None) -> dict[str, Any]:
    """Apply persisted vision.* settings defaults; template params win on conflict."""
    merged: dict[str, Any] = {}
    if ctx is not None:
        try:
            from plugin.framework.config import get_config

            for key in VISION_CONFIG_KEYS:
                val = get_config(ctx, f"vision.{key}")
                if val is not None and val != "":
                    merged[key] = val
        except Exception:
            pass
    if isinstance(template_params, dict):
        merged.update(template_params)
    return merged


def _ok_result(helper: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "helper": helper, **payload}


def _error_result(code: str, message: str, *, helper: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    if details:
        out["details"] = details
    return out


def is_css_inline_import_error(exc: BaseException) -> bool:
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    msg = str(root).lower()
    return "css_inline" in msg or "css-inline" in msg


def css_inline_unavailable_result(helper: str) -> dict[str, Any]:
    from plugin.scripting.vision_html_export import CSS_INLINE_INSTALL_CMD

    return _error_result(
        "CSS_INLINE_UNAVAILABLE",
        f"Install css-inline in your venv (Settings → Python): {CSS_INLINE_INSTALL_CMD}",
        helper=helper,
    )


def _box_to_xywh(box_points: Any) -> list[int]:
    """Convert quadrilateral corners to [x, y, w, h] in PNG pixel space."""
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


def _bbox_to_xywh(bbox: Any) -> list[int]:
    """Normalize bbox to [x, y, w, h] from quad, xyxy, xywh, or Docling l/t/r/b dict."""
    if isinstance(bbox, dict):
        for keys in (("l", "t", "r", "b"), ("x", "y", "w", "h"), ("left", "top", "right", "bottom")):
            if all(k in bbox for k in keys):
                a, b, c, d = (float(bbox[keys[0]]), float(bbox[keys[1]]), float(bbox[keys[2]]), float(bbox[keys[3]]))
                if keys[2] in ("r", "right"):
                    return [int(a), int(b), int(max(0, c - a)), int(max(0, d - b))]
                return [int(a), int(b), int(max(0, c)), int(max(0, d))]
    if not isinstance(bbox, (list, tuple)) or not bbox:
        return [0, 0, 0, 0]
    if len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
        x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        if x1 >= x0 and y1 >= y0 and (x1 - x0) > 1 and (y1 - y0) > 1:
            return [int(x0), int(y0), int(max(0, x1 - x0)), int(max(0, y1 - y0))]
        return [int(x0), int(y0), int(max(0, x1)), int(max(0, y1))]
    return _box_to_xywh(bbox)


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


def _prov_bbox_to_xywh(prov: Any) -> list[int]:
    """Extract [x,y,w,h] from Docling provenance list or dict."""
    if isinstance(prov, list) and prov:
        prov = prov[0]
    if not isinstance(prov, dict):
        return [0, 0, 0, 0]
    bbox = prov.get("bbox") or prov.get("box")
    if bbox is None:
        return [0, 0, 0, 0]
    return _bbox_to_xywh(bbox)


def resolve_engine(params: dict[str, Any]) -> str:
    return str(params.get("engine") or DEFAULT_ENGINE).strip().lower() or DEFAULT_ENGINE


def resolve_ocr_backend(params: dict[str, Any]) -> str:
    return str(params.get("ocr_backend") or DEFAULT_OCR_BACKEND).strip().lower() or DEFAULT_OCR_BACKEND


def fallback_engine_enabled(params: dict[str, Any]) -> bool:
    value = params.get("fallback_engine", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)
