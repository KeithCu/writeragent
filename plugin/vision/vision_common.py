# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers for trusted vision backends (Paddle, Docling)."""

from __future__ import annotations

import io
import logging
from typing import Any

log = logging.getLogger(__name__)

HELPER_NAMES = frozenset({"extract_text", "extract_structure", "detect_objects", "detect_layout", "recognize_pipeline", "perceptual_hash"})

IMPLEMENTED_HELPERS = frozenset({"extract_text", "extract_structure"})

DEFAULT_ENGINE = "docling"
ENGINES = frozenset({"docling", "paddle"})
DEFAULT_OCR_BACKEND = "rapidocr"

# Financial statements and comparison tables often exceed 50 body rows.
MAX_TABLE_ROWS = 200

# plugin.scripting.ipc.DEFAULT_MAX_PAYLOAD_BYTES is 16 MiB. Leave 1 MiB for the
# spec/context pickle wrapper so a max-size image still fits in one frame.
VISION_IMAGE_MAX_BYTES = 15 * 1024 * 1024

# PDF magic so corpus / nearby-file bytes can skip the image OCR raster path.
_PDF_MAGIC = b"%PDF"

# Config keys merged from Settings → vision.* before template param overrides.
VISION_CONFIG_KEYS = ("images_scale", "text_score", "force_full_page_ocr", "table_mode", "do_cell_matching", "create_orphan_clusters", "layout_model", "do_formula_enrichment", "do_code_enrichment", "document_timeout", "artifacts_path", "insert_mode")

VISION_INSERT_MODES = frozenset({"html", "structured"})
DEFAULT_VISION_INSERT_MODE = "html"


def merge_vision_params(ctx: Any = None, template_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply persisted vision.* settings defaults; template params win on conflict."""
    # get_config takes no ctx, so read Settings even when ctx is None.
    # Log config failures instead of swallowing them.
    merged: dict[str, Any] = {}
    try:
        from plugin.framework.config import get_config

        for key in VISION_CONFIG_KEYS:
            val = get_config(f"vision.{key}")
            if val is not None and val != "":
                merged[key] = val
    except Exception:
        log.exception("Failed to load vision.* configuration settings")
    if isinstance(template_params, dict):
        merged.update(template_params)
    return merged


def resolve_vision_insert_mode(ctx: Any = None, template_params: dict[str, Any] | None = None) -> str:
    """Return html (Docling export) or structured (bbox layout / Calc cell grid)."""
    merged = merge_vision_params(ctx, template_params)
    mode = str(merged.get("insert_mode") or DEFAULT_VISION_INSERT_MODE).strip().lower()
    if mode in VISION_INSERT_MODES:
        return mode
    return DEFAULT_VISION_INSERT_MODE


def ok_result(helper: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "helper": helper, **payload}


def error_result(code: str, message: str, *, helper: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
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


CSS_INLINE_INSTALL_CMD = "pip install css-inline"


def css_inline_unavailable_result(helper: str) -> dict[str, Any]:
    return error_result("CSS_INLINE_UNAVAILABLE", f"Install css-inline in your venv (Settings → Python): {CSS_INLINE_INSTALL_CMD}", helper=helper)


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


def bbox_to_xywh(bbox: Any, *, page_height: float | None = None) -> list[int]:
    """Normalize bbox to [x, y, w, h] from quad, xyxy, xywh, or Docling l/t/r/b dict."""
    # BOTTOMLEFT boxes have top > bottom, so max(0, d - b) is 0 height and y
    # is inverted. Height is max(0, abs(d - b)); flip with page_height when
    # it is available.
    # TODO: live run check - verify multi-page BOTTOMLEFT flipping against live Docling PDF pages.
    if isinstance(bbox, dict):
        coord_origin = str(bbox.get("coord_origin") or "").strip().upper()
        for keys in (("l", "t", "r", "b"), ("x", "y", "w", "h"), ("left", "top", "right", "bottom")):
            if all(k in bbox for k in keys):
                a, b, c, d = (float(bbox[keys[0]]), float(bbox[keys[1]]), float(bbox[keys[2]]), float(bbox[keys[3]]))
                if keys[2] in ("r", "right"):
                    w = max(0, c - a)
                    if coord_origin in ("BOTTOMLEFT", "BOTTOM_LEFT") or b > d:
                        h = max(0, b - d) if b > d else max(0, d - b)
                        top_y = (page_height - max(b, d)) if page_height is not None else min(b, d)
                    else:
                        h = max(0, d - b)
                        top_y = b
                    return [int(a), int(top_y), int(w), int(h)]
                return [int(a), int(b), int(max(0, c)), int(max(0, d))]
    elif hasattr(bbox, "l") and hasattr(bbox, "t") and hasattr(bbox, "r") and hasattr(bbox, "b"):
        coord_origin = str(getattr(bbox, "coord_origin", "") or "").strip().upper()
        a, b, c, d = float(bbox.l), float(bbox.t), float(bbox.r), float(bbox.b)
        w = max(0, c - a)
        if coord_origin in ("BOTTOMLEFT", "BOTTOM_LEFT") or b > d:
            h = max(0, b - d) if b > d else max(0, d - b)
            top_y = (page_height - max(b, d)) if page_height is not None else min(b, d)
        else:
            h = max(0, d - b)
            top_y = b
        return [int(a), int(top_y), int(w), int(h)]

    if not isinstance(bbox, (list, tuple)) or not bbox:
        return [0, 0, 0, 0]
    if len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
        x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        if x1 >= x0 and y1 >= y0 and (x1 - x0) > 1 and (y1 - y0) > 1:
            return [int(x0), int(y0), int(max(0, x1 - x0)), int(max(0, y1 - y0))]
        return [int(x0), int(y0), int(max(0, x1)), int(max(0, y1))]
    return _box_to_xywh(bbox)


def decode_image_bytes(image: Any) -> Any:
    """Return a numpy RGB array from raw PNG/JPEG bytes."""
    # Transpose EXIF orientation, then composite transparent pixels onto white.
    # convert('RGB') drops alpha and paints transparent images black-on-black.
    if image is None:
        raise ValueError("image bytes are required")
    if not isinstance(image, (bytes, bytearray)):
        raise ValueError("image must be raw bytes")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ImportError("Pillow is required to decode image bytes for OCR") from exc
    import numpy as np

    with Image.open(io.BytesIO(bytes(image))) as raw_img:
        transposed = ImageOps.exif_transpose(raw_img)
        img = transposed if transposed is not None else raw_img
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            rgb = Image.alpha_composite(bg, rgba).convert("RGB")
        else:
            rgb = img.convert("RGB")
        return np.array(rgb)


def prov_bbox_to_xywh(prov: Any, *, page_height: float | None = None) -> list[int]:
    """Extract [x,y,w,h] from Docling provenance list or dict."""
    if isinstance(prov, list) and prov:
        prov = prov[0]
    if not isinstance(prov, dict) and not hasattr(prov, "bbox"):
        return [0, 0, 0, 0]
    bbox = prov.get("bbox") if isinstance(prov, dict) else getattr(prov, "bbox", None)
    if bbox is None:
        bbox = prov.get("box") if isinstance(prov, dict) else None
    if bbox is None:
        return [0, 0, 0, 0]
    return bbox_to_xywh(bbox, page_height=page_height)


def resolve_engine(params: dict[str, Any]) -> str:
    """Resolve vision engine name, raising ValueError on unknown engines."""
    # Unknown engine names raise ValueError. Falling back to DEFAULT_ENGINE
    # turned a typo like 'padle' into a silent Docling run.
    raw = params.get("engine")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_ENGINE
    engine = str(raw).strip().lower()
    if engine not in ENGINES:
        raise ValueError(f"Unknown vision engine {engine!r}; expected one of {sorted(ENGINES)}")
    return engine


def detect_vision_input_format(data: Any, params: dict[str, Any] | None = None) -> str:
    """Return ``pdf`` or ``image`` for trusted vision bytes.

    ``params["format"]`` of ``pdf`` / ``image`` wins; ``auto`` (default) sniffs
    PDF magic so vector PDFs use Docling's PDF backend instead of IMAGE OCR.
    """
    explicit = str((params or {}).get("format") or "auto").strip().lower()
    if explicit in ("pdf", "image"):
        return explicit
    if isinstance(data, (bytes, bytearray)) and bytes(data[:4]) == _PDF_MAGIC:
        return "pdf"
    return "image"


def resolve_ocr_backend(params: dict[str, Any]) -> str:
    return str(params.get("ocr_backend") or DEFAULT_OCR_BACKEND).strip().lower() or DEFAULT_OCR_BACKEND


def fallback_engine_enabled(params: dict[str, Any]) -> bool:
    value = params.get("fallback_engine", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text") or cell.get("value") or "").strip()
    return str(getattr(cell, "text", "") or "").strip()


def _cell_int(cell: Any, *names: str, default: int = 0) -> int:
    for name in names:
        if isinstance(cell, dict) and name in cell:
            try:
                return int(cell[name])
            except (TypeError, ValueError):
                return default
        value = getattr(cell, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
    return default


def table_from_span_cells(
    cells: list[Any],
    num_rows: int,
    num_cols: int,
    *,
    name: str,
) -> dict[str, Any] | None:
    """Build a rectangular table dict with spans from cell objects with row/col offsets."""
    if num_rows <= 0 or num_cols <= 0:
        return None
    grid: list[list[str]] = [["" for _ in range(num_cols)] for _ in range(num_rows)]
    spans: list[dict[str, int]] = []
    for cell in cells:
        row = _cell_int(cell, "start_row_offset_idx", "start_row", default=-1)
        col = _cell_int(cell, "start_col_offset_idx", "start_col", default=-1)
        rowspan = max(_cell_int(cell, "row_span", default=1), 1)
        colspan = max(_cell_int(cell, "col_span", default=1), 1)
        if row < 0 or col < 0 or row >= num_rows or col >= num_cols:
            continue
        grid[row][col] = _cell_text(cell)
        if rowspan > 1 or colspan > 1:
            spans.append({"row": row, "col": col, "rowspan": rowspan, "colspan": colspan})

    columns = [str(c) for c in grid[0]]
    data_rows = [[str(c) for c in row] for row in grid[1:]]
    limited = data_rows[:MAX_TABLE_ROWS]
    last_kept = len(limited)
    kept_spans: list[dict[str, int]] = []
    for span in spans:
        row = span["row"]
        if row < 0 or row > last_kept:
            continue
        rowspan = int(span["rowspan"])
        colspan = int(span["colspan"])
        max_rowspan = last_kept - row + 1
        if rowspan > max_rowspan:
            rowspan = max_rowspan
        if rowspan <= 1 and colspan <= 1:
            continue
        if rowspan != span["rowspan"]:
            span = {**span, "rowspan": rowspan}
        kept_spans.append(span)
    return {
        "name": name,
        "columns": columns,
        "rows": limited,
        "spans": kept_spans,
        "truncated": len(data_rows) > MAX_TABLE_ROWS,
        "total_rows": len(data_rows),
    }


def table_to_tsv_lines(table: dict[str, Any]) -> list[str]:
    """Format a table dict into TSV lines, preserving empty cells to avoid column misalignment."""
    # Keep empty cells so columns stay aligned, and share table_to_tsv_lines
    # between Paddle and Docling. Dropping falsy cells with `if str(c)` shifts
    # columns, and keeping only the last row drops the rest of the table.
    lines: list[str] = []
    cols = table.get("columns")
    if isinstance(cols, list) and cols:
        lines.append("\t".join(str(c) for c in cols))
    for row in table.get("rows") or []:
        if isinstance(row, list):
            lines.append("\t".join(str(c) for c in row))
    return lines
