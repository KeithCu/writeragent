# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Docling backend for trusted vision helpers — unified OCR/layout/table pipeline."""
from __future__ import annotations

import importlib
import logging
from dataclasses import astuple, dataclass
from io import BytesIO
from typing import Any, Literal

from plugin.vision.vision_common import (
    MAX_TABLE_ROWS,
    css_inline_unavailable_result,
    detect_vision_input_format,
    error_result,
    is_css_inline_import_error,
    ok_result,
    prov_bbox_to_xywh,
    resolve_ocr_backend,
    table_from_span_cells,
    table_to_tsv_lines,
)

__all__ = [
    "_cell_text",
    "_store_converter",
    "_text_score_for_cache",
    "extract_structure",
    "extract_text",
]

log = logging.getLogger(__name__)

_DOCLING_INSTALL_CMD = "pip install docling rapidocr-paddle numpy pillow css-inline onnxruntime"


class OcrBackendError(ValueError):
    """Raised when the requested OCR backend is not available or unknown."""


class LayoutModelError(RuntimeError):
    """Docling layout model_spec cannot be applied. Do not convert with it."""


class DoclingApiMismatchError(LayoutModelError):
    """Docling internal API version mismatch (e.g. get_engine_config / LayoutModelConfig)."""


@dataclass(frozen=True, slots=True)
class DoclingPipelineParams:
    """Normalized pipeline parameters parsed once for cache key and options application."""
    backend: str
    lang: str
    input_format: str
    images_scale: float
    device: str
    num_threads: int
    table_mode: str
    do_cell_matching: bool
    create_orphan_clusters: bool
    layout_model: str
    do_formula_enrichment: bool
    do_code_enrichment: bool
    text_score: float
    force_full_page_ocr: bool
    ocr_mode: str
    document_timeout: float
    artifacts_path: str

    @classmethod
    def from_params(cls, params: dict[str, Any], *, input_format: str = "image") -> DoclingPipelineParams:
        text_score_val = params.get("text_score")
        text_score = 0.5 if (text_score_val is None or text_score_val == "") else float(text_score_val)
        threads_raw = params.get("num_threads")
        num_threads = int(threads_raw) if threads_raw is not None and str(threads_raw).strip() != "" else 0
        return cls(
            backend=resolve_ocr_backend(params),
            lang=str(params.get("lang") or "en").strip() or "en",
            input_format=input_format,
            images_scale=float(params.get("images_scale") or 1.0),
            device=str(params.get("device") or "").strip(),
            num_threads=num_threads,
            table_mode=str(params.get("table_mode") or "accurate"),
            do_cell_matching=bool(params.get("do_cell_matching", True)),
            create_orphan_clusters=bool(params.get("create_orphan_clusters", True)),
            layout_model=str(params.get("layout_model") or "heron").strip().lower() or "heron",
            do_formula_enrichment=bool(params.get("do_formula_enrichment", False)),
            do_code_enrichment=bool(params.get("do_code_enrichment", False)),
            text_score=text_score,
            force_full_page_ocr=bool(params.get("force_full_page_ocr", True)),
            ocr_mode=str(params.get("ocr_mode") or "").strip().lower(),
            document_timeout=float(params.get("document_timeout") or 0),
            artifacts_path=str(params.get("artifacts_path") or "").strip(),
        )


class _ConverterCache:
    """Single (key, converter) entry cache per worker process."""
    def __init__(self) -> None:
        self._entry: tuple[tuple[Any, ...], Any] | None = None

    def get(self, key: tuple[Any, ...]) -> Any:
        if self._entry is not None and self._entry[0] == key:
            return self._entry[1]
        return None

    def set(self, key: tuple[Any, ...], converter: Any) -> None:
        self._entry = (key, converter)

    def clear(self) -> None:
        self._entry = None

    def __getitem__(self, key: tuple[Any, ...]) -> Any:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key: tuple[Any, ...], converter: Any) -> None:
        self.set(key, converter)

    def __contains__(self, key: tuple[Any, ...]) -> bool:
        return self._entry is not None and self._entry[0] == key

    def __len__(self) -> int:
        return 1 if self._entry is not None else 0

    def __iter__(self):
        if self._entry is not None:
            yield self._entry[0]


_converter_cache = _ConverterCache()


def _store_converter(key: tuple[Any, ...], converter: Any) -> None:
    _converter_cache.set(key, converter)


def _import_docling() -> Any:
    return importlib.import_module("docling.document_converter")


def _cache_key(params: dict[str, Any] | DoclingPipelineParams, input_format: str = "image") -> tuple[Any, ...]:
    norm = params if isinstance(params, DoclingPipelineParams) else DoclingPipelineParams.from_params(params, input_format=input_format)
    return astuple(norm)


def _text_score_for_cache(params: dict[str, Any] | DoclingPipelineParams) -> float:
    if isinstance(params, DoclingPipelineParams):
        return params.text_score
    raw = params.get("text_score")
    if raw is None or str(raw).strip() == "":
        return 0.5
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.5


def _resolve_ocr_options(backend_or_params: str | dict[str, Any] | DoclingPipelineParams, lang: str = "en", text_score: float | None = None) -> Any:
    """Build Docling OcrOptions for the requested backend, raising OcrBackendError on failure."""
    if isinstance(backend_or_params, DoclingPipelineParams):
        backend = backend_or_params.backend
        lang = backend_or_params.lang
        text_score = backend_or_params.text_score
    elif isinstance(backend_or_params, dict):
        norm = DoclingPipelineParams.from_params(backend_or_params)
        backend = norm.backend
        lang = norm.lang
        text_score = norm.text_score
    else:
        backend = backend_or_params

    try:
        pipeline_options_mod = importlib.import_module("docling.datamodel.pipeline_options")
    except ImportError as exc:
        raise ImportError("docling.datamodel.pipeline_options is unavailable") from exc

    if backend == "auto":
        return None

    if backend in ("rapidocr", "rapidocr_paddle", "rapidocr_onnx", "rapidocr_openvino", "rapidocr_torch"):
        try:
            rapid_cls = pipeline_options_mod.RapidOcrOptions
            backend_map: dict[str, Literal["onnxruntime", "openvino", "paddle", "torch"]] = {
                "rapidocr": "onnxruntime",
                "rapidocr_paddle": "paddle",
                "rapidocr_onnx": "onnxruntime",
                "rapidocr_openvino": "openvino",
                "rapidocr_torch": "torch",
            }
            ocr_opts = rapid_cls(backend=backend_map.get(backend, "paddle"))
            if hasattr(ocr_opts, "lang"):
                ocr_opts.lang = [lang]
            if text_score is not None and hasattr(ocr_opts, "text_score"):
                ocr_opts.text_score = float(text_score)
            return ocr_opts
        except (ImportError, AttributeError) as exc:
            raise OcrBackendError(f"OCR backend {backend!r} is not available: {exc}") from exc

    if backend == "easyocr":
        try:
            easy_cls = pipeline_options_mod.EasyOcrOptions
            return easy_cls(lang=[lang])
        except (ImportError, AttributeError) as exc:
            raise OcrBackendError(f"EasyOCR backend is not available: {exc}") from exc

    if backend == "tesseract":
        try:
            tess_cls = pipeline_options_mod.TesseractOcrOptions
            return tess_cls(lang=[lang])
        except (ImportError, AttributeError) as exc:
            raise OcrBackendError(f"Tesseract backend is not available: {exc}") from exc

    if backend == "surya":
        try:
            surya_mod = importlib.import_module("docling_surya")
            return surya_mod.SuryaOcrOptions(lang=[lang])
        except ImportError as exc:
            raise OcrBackendError(
                "Surya OCR backend is not installed. pip install docling-surya surya-ocr — or choose another ocr_backend."
            ) from exc

    raise OcrBackendError(f"Unknown ocr_backend {backend!r}")


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


def _resolve_layout_model_spec(layout_key: str) -> Any:
    """Legacy LayoutModelConfig from layout_model_specs (Docling <= 2.117 LayoutOptions)."""
    layout_specs = importlib.import_module("docling.datamodel.layout_model_specs")
    mapping = {
        "heron": layout_specs.DOCLING_LAYOUT_HERON,
        "egret": layout_specs.DOCLING_LAYOUT_EGRET,
        "egret_xlarge": layout_specs.DOCLING_LAYOUT_EGRET_XLARGE,
        "v2": layout_specs.DOCLING_LAYOUT_V2,
    }
    spec = mapping.get(layout_key)
    if spec is None:
        raise LayoutModelError(
            f"Unknown layout_model {layout_key!r}; expected one of {sorted(mapping)}"
        )
    return spec


_OD_LAYOUT_PRESETS: dict[str, tuple[str, str]] = {
    "heron": ("layout_heron", "OBJECT_DETECTION_LAYOUT_HERON"),
    "egret": ("layout_egret", "OBJECT_DETECTION_LAYOUT_EGRET"),
    "egret_xlarge": ("layout_egret_xlarge", "OBJECT_DETECTION_LAYOUT_EGRET_XLARGE"),
    "v2": ("layout_v2", "OBJECT_DETECTION_LAYOUT_V2"),
}


def _resolve_od_layout_model_spec(layout_key: str) -> Any:
    """ObjectDetectionModelSpec for Docling >= 2.118."""
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
        legacy = _resolve_layout_model_spec(layout_key)
        return od_spec_cls(
            name=str(getattr(legacy, "name", "") or "layout_heron"),
            repo_id=str(getattr(legacy, "repo_id", "") or "docling-project/docling-layout-heron"),
            revision=str(getattr(legacy, "revision", "") or "main"),
        )

    raise RuntimeError("Docling ObjectDetectionModelSpec is unavailable")


def _apply_pipeline_params(pipeline_options: Any, params: dict[str, Any] | DoclingPipelineParams) -> None:
    """Map normalized params onto Docling PdfPipelineOptions."""
    norm = params if isinstance(params, DoclingPipelineParams) else DoclingPipelineParams.from_params(params)
    pipeline_options.images_scale = norm.images_scale
    if norm.document_timeout > 0:
        pipeline_options.document_timeout = norm.document_timeout
    else:
        pipeline_options.document_timeout = None

    if norm.artifacts_path:
        pipeline_options.artifacts_path = norm.artifacts_path

    pipeline_options.do_formula_enrichment = norm.do_formula_enrichment
    pipeline_options.do_code_enrichment = norm.do_code_enrichment

    if norm.device:
        acc = getattr(pipeline_options, "accelerator_options", None)
        if acc is not None:
            acc.device = norm.device
    if norm.num_threads > 0:
        acc = getattr(pipeline_options, "accelerator_options", None)
        if acc is not None:
            acc.num_threads = norm.num_threads

    table_opts = pipeline_options.table_structure_options
    if norm.table_mode == "fast":
        table_former = importlib.import_module("docling.datamodel.pipeline_options").TableFormerMode
        table_opts.mode = table_former.FAST
    table_opts.do_cell_matching = norm.do_cell_matching

    layout_opts = pipeline_options.layout_options
    if hasattr(layout_opts, "create_orphan_clusters"):
        layout_opts.create_orphan_clusters = norm.create_orphan_clusters
    if hasattr(layout_opts, "model_spec"):
        if _is_object_detection_layout(layout_opts):
            layout_opts.model_spec = _resolve_od_layout_model_spec(norm.layout_model)
            spec = layout_opts.model_spec
            get_engine = getattr(type(spec), "get_engine_config", None)
            if not callable(get_engine):
                raise DoclingApiMismatchError(
                    "layout model_spec is missing get_engine_config; refusing LayoutModelConfig on OD options"
                )
        else:
            layout_opts.model_spec = _resolve_layout_model_spec(norm.layout_model)


def _want_full_page_ocr(params: dict[str, Any] | DoclingPipelineParams, input_format: str = "image") -> bool:
    """Images default to full-page OCR; PDFs keep native text unless overridden."""
    if isinstance(params, DoclingPipelineParams):
        norm = params
    else:
        norm = DoclingPipelineParams.from_params(params, input_format=input_format)
    explicit_mode = norm.ocr_mode
    if explicit_mode in ("full_page", "full-page"):
        return True
    if explicit_mode in ("default", "layout_regions", "pdf_aware_layout_regions"):
        return False
    if norm.input_format == "pdf":
        return False
    return norm.force_full_page_ocr


def _apply_ocr_mode(pipeline_options: Any, norm: DoclingPipelineParams) -> None:
    """Prefer Docling ``OcrMode.FULL_PAGE`` over deprecated ``force_full_page_ocr``."""
    ocr_opts = getattr(pipeline_options, "ocr_options", None)
    if ocr_opts is None:
        return
    want_full = _want_full_page_ocr(norm)
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


def _build_pipeline_options(params: dict[str, Any] | DoclingPipelineParams, input_format: str = "image") -> Any:
    norm = params if isinstance(params, DoclingPipelineParams) else DoclingPipelineParams.from_params(params, input_format=input_format)
    pipeline_options_mod = importlib.import_module("docling.datamodel.pipeline_options")
    pdf_opts_cls = pipeline_options_mod.PdfPipelineOptions

    ocr_options = _resolve_ocr_options(norm)

    pipeline_options = pdf_opts_cls(
        do_ocr=True,
        do_table_structure=True,
        allow_external_plugins=True,
    )
    if ocr_options is not None:
        pipeline_options.ocr_options = ocr_options
    if norm.backend == "surya":
        setattr(pipeline_options, "ocr_model", "suryaocr")
    _apply_pipeline_params(pipeline_options, norm)
    _apply_ocr_mode(pipeline_options, norm)
    return pipeline_options


def _get_docling_converter(params: dict[str, Any] | DoclingPipelineParams, input_format: str = "image") -> Any:
    norm = params if isinstance(params, DoclingPipelineParams) else DoclingPipelineParams.from_params(params, input_format=input_format)
    key = _cache_key(norm)
    cached = _converter_cache.get(key)
    if cached is not None:
        return cached

    _import_docling()
    base_models = importlib.import_module("docling.datamodel.base_models")
    converter_mod = importlib.import_module("docling.document_converter")
    input_format_enum = base_models.InputFormat
    document_converter_cls = converter_mod.DocumentConverter

    pipeline_options = _build_pipeline_options(norm)
    if norm.input_format == "pdf":
        format_option_cls = converter_mod.PdfFormatOption
        allowed = input_format_enum.PDF
    else:
        format_option_cls = converter_mod.ImageFormatOption
        allowed = input_format_enum.IMAGE
    converter = document_converter_cls(
        allowed_formats=[allowed],
        format_options={allowed: format_option_cls(pipeline_options=pipeline_options)},
    )
    _converter_cache.set(key, converter)
    return converter


def _convert_image_bytes(image: Any, params: dict[str, Any]) -> Any:
    if image is None or not isinstance(image, (bytes, bytearray)):
        raise ValueError("image must be raw bytes")

    payload = bytes(image)
    input_format = detect_vision_input_format(payload, params)
    norm = DoclingPipelineParams.from_params(params, input_format=input_format)
    _import_docling()
    base_models = importlib.import_module("docling.datamodel.base_models")
    buf = BytesIO(payload)
    buf.seek(0)
    stream_name = "document.pdf" if input_format == "pdf" else "image.png"
    stream = base_models.DocumentStream(name=stream_name, stream=buf)
    converter = _get_docling_converter(norm)
    try:
        result = converter.convert(stream)
    except AttributeError as exc:
        msg = str(exc)
        if "get_engine_config" in msg or "LayoutModelConfig" in msg:
            raise DoclingApiMismatchError(f"Docling layout API mismatch: {exc}") from exc
        raise

    document = getattr(result, "document", None)
    if document is None:
        raise RuntimeError("Docling conversion returned no document")
    return document


def _cell_text(cell: Any) -> str:
    # Empty cell text is "", not missing. Returning the TableCell falls through
    # to its str repr. Return "" when text is falsy.
    if isinstance(cell, dict):
        return str(cell.get("text") or cell.get("value") or "").strip()
    return str(getattr(cell, "text", "") or "").strip()


def _map_docling_document(
    document: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Export document dict once and map text regions, structure blocks, tables, and text parts."""
    doc_dict: dict[str, Any] = {}
    if hasattr(document, "export_to_dict"):
        try:
            exported = document.export_to_dict()
            if isinstance(exported, dict):
                doc_dict = exported
        except Exception:
            log.debug("export_to_dict failed", exc_info=True)

    regions: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []

    # Map text items
    for item in doc_dict.get("texts") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        box = prov_bbox_to_xywh(item.get("prov"))
        confidence = float(item.get("confidence") or item.get("score") or 0.0)
        regions.append({"box": box, "text": text, "confidence": confidence})
        label = str(item.get("label") or item.get("type") or "text").strip().lower()
        blocks.append({"type": label or "text", "text": text, "box": box})
        text_parts.append(text)

    # Map tables
    tables: list[dict[str, Any]] = []
    live_tables = getattr(document, "tables", None)
    table_items = live_tables if (isinstance(live_tables, list) and live_tables) else (doc_dict.get("tables") or [])
    table_index = 0
    for item in table_items:
        table_index += 1
        table = None
        box = [0, 0, 0, 0]
        if hasattr(item, "data"):
            data = getattr(item, "data", None)
            cells = getattr(data, "table_cells", None) if data is not None else None
            num_rows = getattr(data, "num_rows", None) if data is not None else None
            num_cols = getattr(data, "num_cols", None) if data is not None else None
            if cells and num_rows and num_cols:
                table = table_from_span_cells(list(cells), int(num_rows), int(num_cols), name=f"table_{table_index}")
        elif isinstance(item, dict):
            data = item.get("data") if isinstance(item.get("data"), dict) else item
            if isinstance(data, dict):
                cells = data.get("table_cells")
                num_rows = data.get("num_rows")
                num_cols = data.get("num_cols")
                if isinstance(cells, list) and cells and num_rows and num_cols:
                    table = table_from_span_cells(cells, int(num_rows), int(num_cols), name=f"table_{table_index}")
                elif "grid" in data and isinstance(data["grid"], list) and data["grid"]:
                    grid = data["grid"]
                    cols = [str(c) for c in grid[0]]
                    rows = [[str(c) for c in r] for r in grid[1:]]
                    table = {
                        "name": f"table_{table_index}",
                        "columns": cols,
                        "rows": rows[:MAX_TABLE_ROWS],
                        "spans": [],
                        "truncated": len(rows) > MAX_TABLE_ROWS,
                        "total_rows": len(rows),
                    }

        # TableItem has no export_to_dict; that call failed and boxes became
        # [0, 0, 0, 0]. Read coordinates from item.prov.
        prov = getattr(item, "prov", None) if not isinstance(item, dict) else item.get("prov")
        if prov:
            box = prov_bbox_to_xywh(prov)

        block_text = ""
        if table:
            tables.append(table)
            table_lines = table_to_tsv_lines(table)
            text_parts.extend(table_lines)
            block_text = "\n".join(table_lines)
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

    return doc_dict, regions, blocks, tables, text_parts


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
    return error_result(
        "DOCLING_UNAVAILABLE",
        (
            f"Docling failed to load in the vision worker: {root_msg}. "
            "Settings → Python → Test checks `import docling.document_converter` (not just docling). "
            f"Install/repair in your venv: {_DOCLING_INSTALL_CMD}"
        ),
        helper=helper,
        details={"import_error": root_msg},
    )


def _run_docling_conversion(
    image: Any,
    params: dict[str, Any],
    helper: str,
) -> tuple[dict[str, Any] | None, Any, str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    # Only OcrBackendError maps to OCR_BACKEND_UNAVAILABLE. Other ValueErrors
    # (bad parameters, pydantic validation) are INVALID_PARAMS.
    try:
        document = _convert_image_bytes(image, params)
        from plugin.vision.venv.vision_html_export import export_docling_to_html

        html = export_docling_to_html(document, params)
        _doc_dict, regions, blocks, tables, text_parts = _map_docling_document(document)
        return None, document, html, regions, blocks, tables, text_parts
    except ImportError as exc:
        if is_css_inline_import_error(exc):
            return css_inline_unavailable_result(helper), None, "", [], [], [], []
        return _handle_docling_import_error(exc, helper=helper), None, "", [], [], [], []
    except OcrBackendError as exc:
        return error_result("OCR_BACKEND_UNAVAILABLE", str(exc), helper=helper), None, "", [], [], [], []
    except DoclingApiMismatchError as exc:
        return error_result("DOCLING_API_MISMATCH", str(exc), helper=helper), None, "", [], [], [], []
    except LayoutModelError as exc:
        return error_result("LAYOUT_MODEL_UNAVAILABLE", str(exc), helper=helper), None, "", [], [], [], []
    except AttributeError as exc:
        if "get_engine_config" in str(exc):
            return error_result("DOCLING_API_MISMATCH", str(exc), helper=helper), None, "", [], [], [], []
        log.exception("Docling %s failed", helper)
        return error_result("VISION_ERROR", str(exc), helper=helper), None, "", [], [], [], []
    except ValueError as exc:
        return error_result("INVALID_PARAMS", str(exc), helper=helper), None, "", [], [], [], []
    except Exception as exc:
        log.exception("Docling %s failed", helper)
        return error_result("VISION_ERROR", str(exc), helper=helper), None, "", [], [], [], []


def extract_text(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_text"
    err, _document, html, regions, _blocks, tables, text_parts = _run_docling_conversion(image, params, helper)
    if err is not None:
        return err

    # full_text includes every text part, tables included. Docling has no
    # mean_confidence, so that field is omitted.
    full_text = "\n".join(text_parts)
    warnings: list[str] = []
    if not full_text:
        warnings.append("No text detected.")

    line_count = len(regions) if regions else (0 if not full_text else len(full_text.splitlines()))

    metrics = _metrics_base(params, image)
    metrics.update({"line_count": line_count, "table_count": len(tables)})

    return ok_result(
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
    err, _document, html, _regions, blocks, tables, text_parts = _run_docling_conversion(image, params, helper)
    if err is not None:
        return err

    full_text = "\n".join(text_parts)
    warnings: list[str] = []
    if not full_text and not tables and not blocks:
        warnings.append("No structure detected.")

    metrics = _metrics_base(params, image)
    metrics.update({"block_count": len(blocks), "table_count": len(tables)})

    return ok_result(
        helper,
        html=html,
        full_text=full_text,
        blocks=blocks,
        tables=tables,
        metrics=metrics,
        warnings=warnings,
    )
