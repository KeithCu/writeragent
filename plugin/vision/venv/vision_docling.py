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


def _cache_key(params: dict[str, Any], *, for_structure: bool) -> tuple[Any, ...]:
    backend = resolve_ocr_backend(params)
    lang = str(params.get("lang") or "en").strip() or "en"
    return (
        backend,
        lang,
        for_structure,
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
        if bool(params.get("force_full_page_ocr", False)) and hasattr(ocr_opts, "force_full_page_ocr"):
            ocr_opts.force_full_page_ocr = True
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


def _resolve_layout_model_spec(params: dict[str, Any]) -> Any:
    layout_key = str(params.get("layout_model") or "heron").strip().lower() or "heron"
    layout_specs = importlib.import_module("docling.datamodel.layout_model_specs")
    mapping = {
        "heron": layout_specs.DOCLING_LAYOUT_HERON,
        "egret_large": getattr(layout_specs, "DOCLING_LAYOUT_EGRET_LARGE", layout_specs.DOCLING_LAYOUT_HERON),
    }
    return mapping.get(layout_key, layout_specs.DOCLING_LAYOUT_HERON)


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
            layout_opts.model_spec = _resolve_layout_model_spec(params)
        except Exception:
            log.debug("layout_model spec resolution failed", exc_info=True)

    del for_structure  # table structure enabled at construction time


def _build_pipeline_options(params: dict[str, Any], *, for_structure: bool) -> Any:
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
        if bool(params.get("force_full_page_ocr", False)) and hasattr(pipeline_options.ocr_options, "force_full_page_ocr"):
            pipeline_options.ocr_options.force_full_page_ocr = True
    if backend == "surya":
        # Docling sets this at runtime for the Surya plugin; stubs omit ocr_model.
        setattr(pipeline_options, "ocr_model", "suryaocr")
    _apply_pipeline_params(pipeline_options, params, for_structure=for_structure)
    return pipeline_options


def _get_docling_converter(params: dict[str, Any], *, for_structure: bool) -> Any:
    key = _cache_key(params, for_structure=for_structure)
    cached = _converter_cache.get(key)
    if cached is not None:
        return cached

    _import_docling()
    base_models = importlib.import_module("docling.datamodel.base_models")
    converter_mod = importlib.import_module("docling.document_converter")
    input_format = base_models.InputFormat
    image_format_option = converter_mod.ImageFormatOption
    document_converter_cls = converter_mod.DocumentConverter

    pipeline_options = _build_pipeline_options(params, for_structure=for_structure)
    converter = document_converter_cls(
        allowed_formats=[input_format.IMAGE],
        format_options={input_format.IMAGE: image_format_option(pipeline_options=pipeline_options)},
    )
    _converter_cache[key] = converter
    return converter


def _convert_image_bytes(image: Any, params: dict[str, Any], *, for_structure: bool) -> Any:
    if image is None or not isinstance(image, (bytes, bytearray)):
        raise ValueError("image must be raw bytes")

    _import_docling()
    base_models = importlib.import_module("docling.datamodel.base_models")
    buf = BytesIO(bytes(image))
    buf.seek(0)
    stream = base_models.DocumentStream(name="image.png", stream=buf)
    converter = _get_docling_converter(params, for_structure=for_structure)
    result = converter.convert(stream)
    document = getattr(result, "document", None)
    if document is None:
        raise RuntimeError("Docling conversion returned no document")
    return document


def _table_from_docling_dict(table_item: dict[str, Any], *, name: str) -> dict[str, Any] | None:
    data = table_item.get("data") if isinstance(table_item.get("data"), dict) else table_item
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
                    text_parts.append("\t".join(str(c) for c in row))
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


def _metrics_base(params: dict[str, Any]) -> dict[str, Any]:
    return {"engine": "docling", "ocr_backend": resolve_ocr_backend(params)}


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

    metrics = _metrics_base(params)
    metrics.update({"line_count": line_count, "mean_confidence": mean_confidence})

    return _ok_result(
        helper,
        html=html,
        full_text=full_text,
        regions=regions,
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

    metrics = _metrics_base(params)
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
