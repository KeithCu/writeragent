# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Docling backend for trusted vision helpers — unified OCR/layout/table pipeline."""
from __future__ import annotations

import importlib
import logging
from io import BytesIO
from typing import Any, Literal

from plugin.vision.vision_common import (
    MAX_TABLE_ROWS,
    css_inline_unavailable_result,
    detect_vision_input_format,
    is_css_inline_import_error,
    _error_result,
    _ok_result,
    _prov_bbox_to_xywh,
    resolve_ocr_backend,
)

log = logging.getLogger(__name__)

_DOCLING_INSTALL_CMD = "pip install docling rapidocr-paddle numpy pillow css-inline"

_converter_cache: dict[tuple[Any, ...], Any] = {}


def _import_docling() -> Any:
    return importlib.import_module("docling.document_converter")


def _cache_key(params: dict[str, Any], *, for_structure: bool, input_format: str) -> tuple[Any, ...]:
    backend = resolve_ocr_backend(params)
    lang = str(params.get("lang") or "en").strip() or "en"
    return (
        backend,
        lang,
        for_structure,
        input_format,
        True,
        float(params.get("images_scale") or 1.0),
        str(params.get("device") or "auto"),
        int(params.get("num_threads") or 4),
        str(params.get("table_mode") or "accurate"),
        bool(params.get("do_cell_matching", True)),
        bool(params.get("create_orphan_clusters", True)),
        str(params.get("layout_model") or "heron"),
        bool(params.get("do_formula_enrichment", False)),
        bool(params.get("do_code_enrichment", False)),
        float(params.get("text_score") or 0.5),
        bool(params.get("force_full_page_ocr", True)),
        str(params.get("ocr_mode") or ""),
        float(params.get("document_timeout") or 0),
        str(params.get("artifacts_path") or ""),
    )


def _resolve_ocr_options(params: dict[str, Any]) -> Any:
    """Build Docling OcrOptions for the requested backend."""
    backend = resolve_ocr_backend(params)
    lang = str(params.get("lang") or "en").strip() or "en"
    try:
        pipeline_options_mod = importlib.import_module("docling.datamodel.pipeline_options")
    except ImportError as exc:
        raise ImportError("docling.datamodel.pipeline_options is unavailable") from exc

    if backend == "auto":
        return None

    if backend in ("rapidocr", "rapidocr_paddle", "rapidocr_onnx", "rapidocr_openvino", "rapidocr_torch"):
        rapid_cls = pipeline_options_mod.RapidOcrOptions
        backend_map: dict[str, Literal["onnxruntime", "openvino", "paddle", "torch"]] = {
            "rapidocr": "onnxruntime",
            "rapidocr_paddle": "paddle",
            "rapidocr_onnx": "onnxruntime",
            "rapidocr_openvino": "openvino",
            "rapidocr_torch": "torch",
        }
        lang = str(params.get("lang") or "en").strip() or "en"
        ocr_opts = rapid_cls(backend=backend_map.get(backend, "paddle"))
        if hasattr(ocr_opts, "lang"):
            ocr_opts.lang = [lang]
        text_score = params.get("text_score")
        if text_score is not None and hasattr(ocr_opts, "text_score"):
            ocr_opts.text_score = float(text_score)
        return ocr_opts

    if backend == "easyocr":
        easy_cls = pipeline_options_mod.EasyOcrOptions
        return easy_cls(lang=[lang])

    if backend == "tesseract":
        tess_cls = pipeline_options_mod.TesseractOcrOptions
        return tess_cls(lang=[lang])

    if backend == "surya":
        try:
            surya_mod = importlib.import_module("docling_surya")
        except ImportError as exc:
            raise ImportError("docling-surya is not installed") from exc
        return surya_mod.SuryaOcrOptions(lang=[lang])

    raise ValueError(f"Unknown ocr_backend {backend!r}")


def _layout_model_key(params: dict[str, Any]) -> str:
    return str(params.get("layout_model") or "heron").strip().lower() or "heron"


def _is_object_detection_layout(layout_opts: Any) -> bool:
    """True when layout_options expects ObjectDetectionModelSpec (Docling >= 2.118)."""
    if getattr(layout_opts, "kind", None) == "layout_object_detection":
        return True
    if type(layout_opts).__name__ == "LayoutObjectDetectionOptions":
        return True
    spec = getattr(layout_opts, "model_spec", None)
    if spec is None:
        return False
    # Inspect the class, not the instance: MagicMock instances invent callable attrs.
    get_engine = getattr(type(spec), "get_engine_config", None)
    return callable(get_engine)


def _resolve_layout_model_spec(params: dict[str, Any]) -> Any:
    """Legacy LayoutModelConfig from layout_model_specs (Docling <= 2.117 LayoutOptions)."""
    layout_key = _layout_model_key(params)
    layout_specs = importlib.import_module("docling.datamodel.layout_model_specs")
    mapping = {
        "heron": layout_specs.DOCLING_LAYOUT_HERON,
        "egret_large": getattr(layout_specs, "DOCLING_LAYOUT_EGRET_LARGE", layout_specs.DOCLING_LAYOUT_HERON),
    }
    return mapping.get(layout_key, layout_specs.DOCLING_LAYOUT_HERON)


_OD_LAYOUT_PRESETS: dict[str, tuple[str, str]] = {
    # WriterAgent layout_model -> (from_preset id, stage_model_specs attribute)
    "heron": ("layout_heron_default", "OBJECT_DETECTION_LAYOUT_HERON"),
    "egret_large": ("layout_egret_large", "OBJECT_DETECTION_LAYOUT_EGRET_LARGE"),
}


def _resolve_od_layout_model_spec(params: dict[str, Any]) -> Any:
    """ObjectDetectionModelSpec for LayoutObjectDetectionOptions (Docling >= 2.118).

    Never returns LayoutModelConfig — assigning that onto OD options skips
    Docling's LayoutOptions→OD shim and convert then raises AttributeError
    on model_spec.get_engine_config (issue 587).
    """
    layout_key = _layout_model_key(params)
    preset_id, stage_attr = _OD_LAYOUT_PRESETS.get(layout_key, _OD_LAYOUT_PRESETS["heron"])

    pipeline_mod = importlib.import_module("docling.datamodel.pipeline_options")
    od_cls = getattr(pipeline_mod, "LayoutObjectDetectionOptions", None)
    from_preset = getattr(od_cls, "from_preset", None) if od_cls is not None else None
    if callable(from_preset):
        try:
            preset_opts = from_preset(preset_id)
            spec = getattr(preset_opts, "model_spec", None)
            if spec is not None:
                return spec
        except Exception:
            log.debug("LayoutObjectDetectionOptions.from_preset(%s) failed", preset_id, exc_info=True)

    stage_mod = importlib.import_module("docling.datamodel.stage_model_specs")
    preset = getattr(stage_mod, stage_attr, None)
    if preset is None:
        preset = getattr(stage_mod, "OBJECT_DETECTION_LAYOUT_HERON", None)
    spec = getattr(preset, "model_spec", None)
    if spec is not None:
        return spec

    od_spec_cls = getattr(stage_mod, "ObjectDetectionModelSpec", None)
    if callable(od_spec_cls):
        legacy = _resolve_layout_model_spec(params)
        return od_spec_cls(
            name=str(getattr(legacy, "name", "") or "layout_heron"),
            repo_id=str(getattr(legacy, "repo_id", "") or "docling-project/docling-layout-heron"),
            revision=str(getattr(legacy, "revision", "") or "main"),
        )

    raise RuntimeError("Docling ObjectDetectionModelSpec is unavailable")


def _apply_pipeline_params(pipeline_options: Any, params: dict[str, Any], *, for_structure: bool) -> None:
    """Map WriterAgent flat params onto Docling PdfPipelineOptions."""
    scale = params.get("images_scale")
    if scale is not None:
        pipeline_options.images_scale = float(scale)

    doc_timeout = params.get("document_timeout")
    if doc_timeout is not None:
        timeout_val = float(doc_timeout)
        pipeline_options.document_timeout = None if timeout_val <= 0 else timeout_val

    artifacts = str(params.get("artifacts_path") or "").strip()
    if artifacts:
        pipeline_options.artifacts_path = artifacts

    if "do_formula_enrichment" in params:
        pipeline_options.do_formula_enrichment = bool(params.get("do_formula_enrichment"))
    if "do_code_enrichment" in params:
        pipeline_options.do_code_enrichment = bool(params.get("do_code_enrichment"))

    device = str(params.get("device") or "").strip()
    if device:
        acc = pipeline_options.accelerator_options
        acc.device = device
    num_threads = params.get("num_threads")
    if num_threads is not None:
        pipeline_options.accelerator_options.num_threads = int(num_threads)

    table_opts = pipeline_options.table_structure_options
    table_mode = str(params.get("table_mode") or "accurate").strip().lower()
    if table_mode == "fast":
        table_former = importlib.import_module("docling.datamodel.pipeline_options").TableFormerMode
        table_opts.mode = table_former.FAST
    if "do_cell_matching" in params:
        table_opts.do_cell_matching = bool(params.get("do_cell_matching"))

    layout_opts = pipeline_options.layout_options
    if hasattr(layout_opts, "create_orphan_clusters") and "create_orphan_clusters" in params:
        layout_opts.create_orphan_clusters = bool(params.get("create_orphan_clusters"))
    if hasattr(layout_opts, "model_spec"):
        try:
            # Dual-path for Docling layout model_spec (issue 587).
            # Sunset: the legacy LayoutModelConfig / layout_model_specs branch
            # can be removed once WriterAgent assumes Docling >= 2.118.0
            # (released 2026-08-03), when LayoutObjectDetectionOptions became
            # the PdfPipelineOptions default and assigning LayoutModelConfig
            # onto model_spec stopped being valid (convert then calls
            # model_spec.get_engine_config and raises AttributeError).
            if _is_object_detection_layout(layout_opts):
                layout_opts.model_spec = _resolve_od_layout_model_spec(params)
            else:
                layout_opts.model_spec = _resolve_layout_model_spec(params)
        except Exception:
            log.debug("layout_model spec resolution failed", exc_info=True)

    del for_structure  # table structure enabled at construction time


def _want_full_page_ocr(params: dict[str, Any], *, input_format: str) -> bool:
    """Images default to full-page OCR; PDFs keep native text unless overridden.

    Vector PDFs (10-K, arXiv) already have a text layer — forcing full-page OCR
    rasterizes them. Scanned PDFs still OCR via ``do_ocr=True`` when there is no
    text. ``ocr_mode`` / ``force_full_page_ocr`` remain explicit overrides.
    """
    explicit_mode = str(params.get("ocr_mode") or "").strip().lower()
    if explicit_mode in ("full_page", "full-page"):
        return True
    if explicit_mode in ("default", "layout_regions", "pdf_aware_layout_regions"):
        return False
    if input_format == "pdf":
        return False
    return bool(params.get("force_full_page_ocr", True))


def _apply_ocr_mode(pipeline_options: Any, params: dict[str, Any], *, input_format: str) -> None:
    """Prefer Docling ``OcrMode.FULL_PAGE`` over deprecated ``force_full_page_ocr``."""
    ocr_opts = getattr(pipeline_options, "ocr_options", None)
    if ocr_opts is None:
        return
    want_full = _want_full_page_ocr(params, input_format=input_format)
    pipeline_mod = importlib.import_module("docling.datamodel.pipeline_options")
    ocr_mode_cls = getattr(pipeline_mod, "OcrMode", None)
    if ocr_mode_cls is not None and hasattr(ocr_opts, "mode"):
        full_page = getattr(ocr_mode_cls, "FULL_PAGE", None)
        default_mode = getattr(ocr_mode_cls, "DEFAULT", None)
        if want_full and full_page is not None:
            ocr_opts.mode = full_page
            return
        if default_mode is not None:
            ocr_opts.mode = default_mode
            return
    if hasattr(ocr_opts, "force_full_page_ocr"):
        ocr_opts.force_full_page_ocr = want_full


def _build_pipeline_options(
    params: dict[str, Any],
    *,
    for_structure: bool,
    input_format: str = "image",
) -> Any:
    pipeline_options_mod = importlib.import_module("docling.datamodel.pipeline_options")
    pdf_opts_cls = pipeline_options_mod.PdfPipelineOptions
    backend = resolve_ocr_backend(params)

    try:
        ocr_options = _resolve_ocr_options(params)
    except ImportError as exc:
        raise exc
    except ValueError as exc:
        raise exc

    pipeline_options = pdf_opts_cls(
        do_ocr=True,
        do_table_structure=for_structure,
        allow_external_plugins=True,
    )
    if ocr_options is not None:
        pipeline_options.ocr_options = ocr_options
    if backend == "surya":
        # Docling sets this at runtime for the Surya plugin; stubs omit ocr_model.
        setattr(pipeline_options, "ocr_model", "suryaocr")
    _apply_pipeline_params(pipeline_options, params, for_structure=for_structure)
    _apply_ocr_mode(pipeline_options, params, input_format=input_format)
    return pipeline_options


def _get_docling_converter(
    params: dict[str, Any],
    *,
    for_structure: bool,
    input_format: str = "image",
) -> Any:
    key = _cache_key(params, for_structure=for_structure, input_format=input_format)
    cached = _converter_cache.get(key)
    if cached is not None:
        return cached

    _import_docling()
    base_models = importlib.import_module("docling.datamodel.base_models")
    converter_mod = importlib.import_module("docling.document_converter")
    input_format_enum = base_models.InputFormat
    document_converter_cls = converter_mod.DocumentConverter

    pipeline_options = _build_pipeline_options(
        params, for_structure=for_structure, input_format=input_format
    )
    if input_format == "pdf":
        format_option_cls = converter_mod.PdfFormatOption
        allowed = input_format_enum.PDF
    else:
        format_option_cls = converter_mod.ImageFormatOption
        allowed = input_format_enum.IMAGE
    converter = document_converter_cls(
        allowed_formats=[allowed],
        format_options={allowed: format_option_cls(pipeline_options=pipeline_options)},
    )
    _converter_cache[key] = converter
    return converter


def _convert_image_bytes(image: Any, params: dict[str, Any], *, for_structure: bool) -> Any:
    if image is None or not isinstance(image, (bytes, bytearray)):
        raise ValueError("image must be raw bytes")

    payload = bytes(image)
    input_format = detect_vision_input_format(payload, params)
    _import_docling()
    base_models = importlib.import_module("docling.datamodel.base_models")
    buf = BytesIO(payload)
    buf.seek(0)
    # Filename extension must match the sniffed format so Docling's stream
    # MIME guess agrees with allowed_formats (PDF magic vs IMAGE).
    stream_name = "document.pdf" if input_format == "pdf" else "image.png"
    stream = base_models.DocumentStream(name=stream_name, stream=buf)
    converter = _get_docling_converter(
        params, for_structure=for_structure, input_format=input_format
    )
    result = converter.convert(stream)
    document = getattr(result, "document", None)
    if document is None:
        raise RuntimeError("Docling conversion returned no document")
    return document


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text") or cell.get("value") or "").strip()
    return str(getattr(cell, "text", None) or cell or "").strip()


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


def _table_from_span_cells(
    cells: list[Any],
    num_rows: int,
    num_cols: int,
    *,
    name: str,
) -> dict[str, Any] | None:
    """Build a rectangular grid with span metadata; text only in the origin cell.

    Docling's expanded ``grid`` repeats spanned text (``ASSETS:`` in every
    column). Calc/Writer need one origin cell plus colspan/rowspan instead.
    """
    if num_rows <= 0 or num_cols <= 0 or not cells:
        return None
    grid = [[""] * num_cols for _ in range(num_rows)]
    spans: list[dict[str, int]] = []
    for cell in cells:
        row = _cell_int(cell, "start_row_offset_idx", "start_row")
        col = _cell_int(cell, "start_col_offset_idx", "start_col")
        rowspan = max(_cell_int(cell, "row_span", default=1), 1)
        colspan = max(_cell_int(cell, "col_span", default=1), 1)
        if row < 0 or col < 0 or row >= num_rows or col >= num_cols:
            continue
        grid[row][col] = _cell_text(cell)
        if rowspan > 1 or colspan > 1:
            spans.append({"row": row, "col": col, "rowspan": rowspan, "colspan": colspan})

    columns = [str(c) for c in grid[0]]
    data_rows = [[str(c) for c in row] for row in grid[1:]]
    if not columns and data_rows:
        width = max(len(r) for r in data_rows)
        columns = [f"col_{i + 1}" for i in range(width)]
    limited = data_rows[:MAX_TABLE_ROWS]
    # Drop spans that land only in truncated body rows.
    kept_spans = [
        span
        for span in spans
        if span["row"] == 0 or span["row"] - 1 < len(limited)
    ]
    return {
        "name": name,
        "columns": columns,
        "rows": limited,
        "spans": kept_spans,
        "truncated": len(data_rows) > MAX_TABLE_ROWS,
        "total_rows": len(data_rows),
    }


def _table_from_docling_item(table_item: Any, *, name: str) -> dict[str, Any] | None:
    data = getattr(table_item, "data", None)
    if data is not None:
        cells = getattr(data, "table_cells", None)
        num_rows = getattr(data, "num_rows", None)
        num_cols = getattr(data, "num_cols", None)
        if cells and num_rows and num_cols:
            mapped = _table_from_span_cells(list(cells), int(num_rows), int(num_cols), name=name)
            if mapped:
                return mapped
    if isinstance(table_item, dict):
        return _table_from_docling_dict(table_item, name=name)
    dumped = None
    if hasattr(table_item, "export_to_dict"):
        try:
            dumped = table_item.export_to_dict()
        except Exception:
            dumped = None
    if isinstance(dumped, dict):
        return _table_from_docling_dict(dumped, name=name)
    return None


def _table_from_docling_dict(table_item: dict[str, Any], *, name: str) -> dict[str, Any] | None:
    data = table_item.get("data") if isinstance(table_item.get("data"), dict) else table_item
    if isinstance(data, dict):
        cells = data.get("table_cells")
        num_rows = data.get("num_rows")
        num_cols = data.get("num_cols")
        if isinstance(cells, list) and cells and not isinstance(cells[0], list):
            first = cells[0]
            looks_like_cells = isinstance(first, dict) and (
                "start_row_offset_idx" in first or "row_span" in first
            )
            if looks_like_cells:
                if not num_rows or not num_cols:
                    max_r = 0
                    max_c = 0
                    for cell in cells:
                        end_r = _cell_int(cell, "end_row_offset_idx")
                        end_c = _cell_int(cell, "end_col_offset_idx")
                        start_r = _cell_int(cell, "start_row_offset_idx", "start_row")
                        start_c = _cell_int(cell, "start_col_offset_idx", "start_col")
                        rs = max(_cell_int(cell, "row_span", default=1), 1)
                        cs = max(_cell_int(cell, "col_span", default=1), 1)
                        max_r = max(max_r, end_r, start_r + rs)
                        max_c = max(max_c, end_c, start_c + cs)
                    num_rows = int(num_rows or max_r)
                    num_cols = int(num_cols or max_c)
                mapped = _table_from_span_cells(cells, int(num_rows), int(num_cols), name=name)
                if mapped:
                    return mapped

    grid = None
    if isinstance(data, dict):
        grid = data.get("grid") or data.get("table_cells") or data.get("cells")
    if grid is None:
        grid = table_item.get("grid") or table_item.get("cells")

    rows: list[list[str]] = []
    if isinstance(grid, list):
        for row in grid:
            if isinstance(row, list):
                cells = []
                for cell in row:
                    if isinstance(cell, dict):
                        cells.append(str(cell.get("text") or cell.get("value") or "").strip())
                    else:
                        cells.append(str(cell).strip())
                rows.append(cells)
            elif isinstance(row, dict):
                text = str(row.get("text") or row.get("value") or "").strip()
                rows.append([text])

    if not rows:
        return None

    columns = [str(c) for c in rows[0]]
    data_rows = [[str(c) for c in row] for row in rows[1:]]
    if not columns and data_rows:
        width = max(len(r) for r in data_rows)
        columns = [f"col_{i + 1}" for i in range(width)]
    limited = data_rows[:MAX_TABLE_ROWS]
    return {
        "name": name,
        "columns": columns,
        "rows": limited,
        "spans": [],
        "truncated": len(data_rows) > MAX_TABLE_ROWS,
        "total_rows": len(data_rows),
    }


def _map_docling_text(document: Any) -> tuple[str, list[dict[str, Any]]]:
    regions: list[dict[str, Any]] = []
    text_parts: list[str] = []

    doc_dict: dict[str, Any] | None = None
    if hasattr(document, "export_to_dict"):
        try:
            exported = document.export_to_dict()
            if isinstance(exported, dict):
                doc_dict = exported
        except Exception:
            log.debug("export_to_dict failed; falling back to markdown", exc_info=True)

    if doc_dict:
        for item in doc_dict.get("texts") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            box = _prov_bbox_to_xywh(item.get("prov"))
            confidence = float(item.get("confidence") or item.get("score") or 0.0)
            regions.append({"box": box, "text": text, "confidence": confidence})
            text_parts.append(text)

    full_text = "\n".join(text_parts)
    if not full_text and hasattr(document, "export_to_markdown"):
        try:
            full_text = str(document.export_to_markdown() or "").strip()
        except Exception:
            log.debug("export_to_markdown failed", exc_info=True)
    return full_text, regions


def _map_docling_structure(document: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    text_parts: list[str] = []
    table_index = 0

    doc_dict: dict[str, Any] = {}
    if hasattr(document, "export_to_dict"):
        try:
            exported = document.export_to_dict()
            if isinstance(exported, dict):
                doc_dict = exported
        except Exception:
            log.debug("export_to_dict failed for structure", exc_info=True)

    for item in doc_dict.get("texts") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        label = str(item.get("label") or item.get("type") or "text").strip().lower()
        box = _prov_bbox_to_xywh(item.get("prov"))
        blocks.append({"type": label or "text", "text": text, "box": box})
        if text:
            text_parts.append(text)

    live_tables = getattr(document, "tables", None)
    mapped_from_live = False
    if isinstance(live_tables, list) and live_tables:
        for item in live_tables:
            table_index += 1
            table = _table_from_docling_item(item, name=f"table_{table_index}")
            box = [0, 0, 0, 0]
            if hasattr(item, "prov") or hasattr(item, "export_to_dict"):
                try:
                    dumped = item.export_to_dict() if hasattr(item, "export_to_dict") else {}
                    if isinstance(dumped, dict):
                        box = _prov_bbox_to_xywh(dumped.get("prov"))
                except Exception:
                    box = [0, 0, 0, 0]
            block_text = ""
            if table:
                mapped_from_live = True
                tables.append(table)
                if table.get("columns"):
                    text_parts.append("\t".join(str(c) for c in table["columns"]))
                for row in table.get("rows") or []:
                    if isinstance(row, list):
                        text_parts.append("\t".join(str(c) for c in row if str(c)))
                block_text = "\n".join(text_parts[-1:] if text_parts else [])
            blocks.append({"type": "table", "text": block_text, "box": box})

    if not mapped_from_live:
        table_index = 0
        # Drop live-table placeholder blocks if dict mapping will re-add them.
        blocks = [block for block in blocks if str(block.get("type") or "") != "table"]
        for item in doc_dict.get("tables") or []:
            if not isinstance(item, dict):
                continue
            table_index += 1
            box = _prov_bbox_to_xywh(item.get("prov"))
            table = _table_from_docling_dict(item, name=f"table_{table_index}")
            block_text = ""
            if table:
                tables.append(table)
                if table.get("columns"):
                    text_parts.append("\t".join(str(c) for c in table["columns"]))
                for row in table.get("rows") or []:
                    if isinstance(row, list):
                        text_parts.append("\t".join(str(c) for c in row if str(c)))
                block_text = "\n".join(text_parts[-1:] if text_parts else [])
            blocks.append({"type": "table", "text": block_text, "box": box})

    if not text_parts and hasattr(document, "export_to_markdown"):
        try:
            md = str(document.export_to_markdown() or "").strip()
            if md:
                text_parts.append(md)
                if not blocks:
                    blocks.append({"type": "text", "text": md, "box": [0, 0, 0, 0]})
        except Exception:
            log.debug("export_to_markdown failed for structure", exc_info=True)

    return blocks, tables, text_parts


def _metrics_base(params: dict[str, Any], image: Any = None) -> dict[str, Any]:
    metrics: dict[str, Any] = {"engine": "docling", "ocr_backend": resolve_ocr_backend(params)}
    if image is not None:
        metrics["input_format"] = detect_vision_input_format(image, params)
    return metrics


def _root_import_error(exc: BaseException) -> str:
    """Prefer the deepest ImportError message (avoid generic wrappers)."""
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    return str(root)


def _handle_docling_import_error(exc: Exception, *, helper: str) -> dict[str, Any]:
    root_msg = _root_import_error(exc)
    msg = root_msg.lower()
    if "surya" in msg or "docling-surya" in msg:
        return _error_result(
            "OCR_BACKEND_UNAVAILABLE",
            "Surya OCR backend is not installed. pip install docling-surya surya-ocr — or choose another ocr_backend.",
            helper=helper,
            details={"ocr_backend": "surya"},
        )
    if "rapidocr" in msg or "ocr_backend" in msg or "unknown ocr_backend" in msg or "paddle" in msg:
        return _error_result(
            "OCR_BACKEND_UNAVAILABLE",
            f"OCR backend is not available: {root_msg}. "
            "For rapidocr without paddle, set ocr_backend to rapidocr or rapidocr_onnx in the template params. "
            "For rapidocr_paddle: pip install rapidocr-paddle paddlepaddle.",
            helper=helper,
        )
    return _error_result(
        "DOCLING_UNAVAILABLE",
        (
            f"Docling failed to load in the vision worker: {root_msg}. "
            "Settings → Python → Test checks `import docling.document_converter` (not just docling). "
            f"Install/repair in your venv: {_DOCLING_INSTALL_CMD}"
        ),
        helper=helper,
        details={"import_error": root_msg},
    )


def _handle_css_inline_import_error(helper: str) -> dict[str, Any]:
    return css_inline_unavailable_result(helper)


def extract_text(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_text"
    try:
        document = _convert_image_bytes(image, params, for_structure=True)
        from plugin.vision.venv.vision_html_export import export_docling_to_html

        html = export_docling_to_html(document, params)
        full_text, regions = _map_docling_text(document)
        _blocks, tables, _text_parts = _map_docling_structure(document)
    except ImportError as exc:
        if is_css_inline_import_error(exc):
            return _handle_css_inline_import_error(helper)
        return _handle_docling_import_error(exc, helper=helper)
    except ValueError as exc:
        return _error_result("OCR_BACKEND_UNAVAILABLE", str(exc), helper=helper)
    except Exception as exc:
        log.exception("Docling extract_text failed")
        return _error_result("VISION_ERROR", str(exc), helper=helper)

    warnings: list[str] = []
    if not full_text:
        warnings.append("No text detected.")

    confidences = [float(r["confidence"]) for r in regions if r.get("confidence") is not None]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    line_count = len(regions) if regions else (0 if not full_text else len(full_text.splitlines()))

    metrics = _metrics_base(params, image)
    metrics.update({"line_count": line_count, "mean_confidence": mean_confidence, "table_count": len(tables)})

    return _ok_result(
        helper,
        html=html,
        full_text=full_text,
        regions=regions,
        tables=tables,
        metrics=metrics,
        warnings=warnings,
    )


def extract_structure(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_structure"
    try:
        document = _convert_image_bytes(image, params, for_structure=True)
        from plugin.vision.venv.vision_html_export import export_docling_to_html

        html = export_docling_to_html(document, params)
        blocks, tables, text_parts = _map_docling_structure(document)
    except ImportError as exc:
        if is_css_inline_import_error(exc):
            return _handle_css_inline_import_error(helper)
        return _handle_docling_import_error(exc, helper=helper)
    except ValueError as exc:
        return _error_result("OCR_BACKEND_UNAVAILABLE", str(exc), helper=helper)
    except Exception as exc:
        log.exception("Docling extract_structure failed")
        return _error_result("VISION_ERROR", str(exc), helper=helper)

    full_text = "\n".join(text_parts)
    warnings: list[str] = []
    if not full_text and not tables and not blocks:
        warnings.append("No structure detected.")

    metrics = _metrics_base(params, image)
    metrics.update({"block_count": len(blocks), "table_count": len(tables)})

    return _ok_result(
        helper,
        html=html,
        full_text=full_text,
        blocks=blocks,
        tables=tables,
        metrics=metrics,
        warnings=warnings,
    )
