# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Docling backend for trusted vision helpers — unified OCR/layout/table pipeline."""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import re
import shutil
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from plugin.framework.constants import TESSERACT_PROGRAMS
from plugin.vision.venv.vision_html_export import prepare_html_for_lo_import
from plugin.vision.vision_common import (
    LOAD_MODELS_HELPER,
    bbox_to_xywh,
    css_inline_unavailable_result,
    detect_vision_input_format,
    error_result,
    is_css_inline_import_error,
    ok_result,
    resolve_ocr_backend,
    table_from_span_cells,
    table_to_tsv_lines,
    vision_bool,
)

log = logging.getLogger(__name__)

# Floor 2.145: iso: language tags arrived in 2.127, RapidOCR model_size in 2.133, and detect_orientation in 2.145.
_DOCLING_MIN_VERSION: tuple[int, int] = (2, 145)
_DOCLING_REQUIREMENT = "docling>=%d.%d" % _DOCLING_MIN_VERSION
# Double quotes stop the shell reading ">" as a redirect (cmd.exe and POSIX shells alike).
_DOCLING_INSTALL_CMD = f'pip install "{_DOCLING_REQUIREMENT}" onnxruntime css-inline'

# A token that looks like a BCP-47 tag (en, en-US, en_US, zh-Hant-TW, chi_sim) gets Docling's
# "iso:" prefix; Docling maps iso: tags per engine (docling/utils/ocr_language.py
# OcrLanguageResolver.canonicalize_ocr_language). Anything else (japan, latin, chinese_cht,
# script/Cyrillic) is an engine-native code and passes through unchanged.
_BCP47_LIKE = re.compile(r"[A-Za-z]{2,3}([-_][A-Za-z0-9]{1,8})*")


def docling_ocr_langs(raw: Any) -> tuple[str, ...]:
    """Split a comma list, strip, drop empties/duplicates (order kept), add iso: to BCP-47-looking tokens; empty -> ("iso:en",)."""
    if raw is None:
        return ("iso:en",)
    parts = [str(item).strip() for item in raw] if isinstance(raw, (list, tuple)) else [p.strip() for p in str(raw).split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for tok in parts:
        if not tok:
            continue
        if not tok.startswith("iso:") and _BCP47_LIKE.fullmatch(tok):
            tok = f"iso:{tok}"
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return tuple(out) if out else ("iso:en",)


class OcrBackendError(ValueError):
    """Raised when the requested OCR backend is not available or unknown."""


@dataclass(frozen=True, slots=True)
class OcrBackend:
    """One ocr_backend id: the Docling OcrOptions class it builds and what must be installed for it."""

    options_class: str  # attribute of docling.datamodel.pipeline_options
    modules: tuple[str, ...] = ()  # top-level modules that must be importable in the venv
    programs: tuple[str, ...] = ()  # Tesseract CLI: names/paths tried in order with shutil.which
    install_hint: str = ""
    rapidocr_engine: str = ""  # RapidOcrOptions.backend; "" for non-RapidOCR rows


def _rapidocr(engine: str, module: str, pip_name: str) -> OcrBackend:
    return OcrBackend(
        "RapidOcrOptions",
        ("rapidocr", module),
        install_hint=f"pip install rapidocr {pip_name}",
        rapidocr_engine=engine,
    )


# Kept in sync with _OCR_ENGINE_PROBE_BODY in plugin/scripting/venv_diagnostics.py
# (host diagnostics cannot import this venv-side module because LibrePy builds may omit plugin.vision).
OCR_BACKENDS: dict[str, OcrBackend] = {
    "rapidocr": _rapidocr("onnxruntime", "onnxruntime", "onnxruntime"),
    "rapidocr_onnx": _rapidocr("onnxruntime", "onnxruntime", "onnxruntime"),
    "rapidocr_openvino": _rapidocr("openvino", "openvino", "openvino"),
    "rapidocr_paddle": _rapidocr("paddle", "paddle", "paddlepaddle"),
    "rapidocr_torch": _rapidocr("torch", "torch", "torch"),
    "easyocr": OcrBackend("EasyOcrOptions", ("easyocr",), install_hint="pip install easyocr"),
    "tesseract": OcrBackend(
        "TesseractCliOcrOptions",
        programs=TESSERACT_PROGRAMS,
        install_hint=(
            "Install the Tesseract OCR program with its eng and osd language data (Linux: your package manager; "
            "macOS: Homebrew or MacPorts; Windows: the UB Mannheim installer). WriterAgent looks for tesseract "
            "on PATH and in its default install folder."
        ),
    ),
    "nemotron": OcrBackend(
        "NemotronOcrOptions",
        ("nemotron_ocr",),
        install_hint=(
            "pip install nemotron-ocr (Docling runs Nemotron OCR only on Linux x86_64 with an NVIDIA CUDA GPU)."
        ),
    ),
    "ocrmac": OcrBackend("OcrMacOptions", ("ocrmac",), install_hint="pip install ocrmac (macOS only)."),
    "auto": OcrBackend("OcrAutoOptions"),
}


def _installed_ocr_backend(name: str) -> tuple[OcrBackend, str]:
    """Return (row, program) for *name*; raise OcrBackendError when unknown or when its modules/program are missing."""
    row = OCR_BACKENDS.get(name)
    if row is None:
        raise OcrBackendError(
            f"Unknown ocr_backend {name!r}; expected one of: {', '.join(sorted(OCR_BACKENDS))}"
        )
    missing = [m for m in row.modules if importlib.util.find_spec(m) is None]
    program = next((found for found in map(shutil.which, row.programs) if found), "")
    if row.programs and not program:
        missing.append(row.programs[0])
    if missing:
        raise OcrBackendError(
            f"OCR backend {name!r} needs {', '.join(missing)}, which is not installed for the Python venv. {row.install_hint}"
        )
    return row, program


class DoclingApiMismatchError(RuntimeError):
    """Installed Docling is older than _DOCLING_MIN_VERSION; reported as DOCLING_API_MISMATCH."""


def _check_docling_version() -> None:
    """Raise DoclingApiMismatchError when installed Docling is older than _DOCLING_MIN_VERSION.

    Reads the docling distribution, then docling-slim (slim installs ship the code without the
    docling meta-package): the same order as docling/__init__.py _resolve_version. No metadata
    means Docling is not installed; the Docling import that follows reports DOCLING_UNAVAILABLE.
    A version string without major.minor is not blocked.
    """
    for dist in ("docling", "docling-slim"):
        try:
            installed = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            continue
        match = re.match(r"(\d+)\.(\d+)", installed)
        if match and (int(match[1]), int(match[2])) < _DOCLING_MIN_VERSION:
            raise DoclingApiMismatchError(
                f"Docling {installed} is installed, but WriterAgent Vision needs Docling "
                "%d.%d or newer. Upgrade it in the Settings → Python venv: " % _DOCLING_MIN_VERSION
                + f'pip install -U "{_DOCLING_REQUIREMENT}"'
            )
        return


@dataclass(frozen=True, slots=True)
class DoclingPipelineParams:
    """Normalized pipeline parameters parsed once for cache key and options application."""
    backend: str
    lang: tuple[str, ...]
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
    full_page_ocr: bool
    document_timeout: float
    artifacts_path: str
    rapidocr_model_size: str        # "tiny" | "small" | "medium"; Docling validates
    rapidocr_detect_orientation: bool

    @classmethod
    def from_params(cls, params: dict[str, Any], *, input_format: str = "image") -> DoclingPipelineParams:
        text_score_val = params.get("text_score")
        text_score = 0.5 if (text_score_val is None or text_score_val == "") else float(text_score_val)
        threads_raw = params.get("num_threads")
        num_threads = int(threads_raw) if threads_raw is not None and str(threads_raw).strip() != "" else 0
        ocr_mode = str(params.get("ocr_mode") or "").strip().lower()
        if ocr_mode in ("full_page", "full-page"):
            full_page_ocr = True
        elif ocr_mode in ("default", "layout_regions", "pdf_aware_layout_regions") or input_format == "pdf":
            full_page_ocr = False
        else:
            full_page_ocr = vision_bool(params.get("force_full_page_ocr", True))
        return cls(
            backend=resolve_ocr_backend(params),
            lang=docling_ocr_langs(params.get("lang")),
            input_format=input_format,
            images_scale=float(params.get("images_scale") or 1.0),
            device=str(params.get("device") or "").strip(),
            num_threads=num_threads,
            table_mode=str(params.get("table_mode") or "accurate"),
            do_cell_matching=vision_bool(params.get("do_cell_matching", True)),
            create_orphan_clusters=vision_bool(params.get("create_orphan_clusters", True)),
            layout_model=str(params.get("layout_model") or "heron").strip().lower() or "heron",
            do_formula_enrichment=vision_bool(params.get("do_formula_enrichment", False)),
            do_code_enrichment=vision_bool(params.get("do_code_enrichment", False)),
            text_score=text_score,
            full_page_ocr=full_page_ocr,
            document_timeout=float(params.get("document_timeout") or 0),
            artifacts_path=str(params.get("artifacts_path") or "").strip(),
            rapidocr_model_size=str(params.get("rapidocr_model_size") or "small").strip().lower() or "small",
            rapidocr_detect_orientation=vision_bool(params.get("rapidocr_detect_orientation", False)),
        )


# One converter per worker process. A new key replaces the previous entry.
_converter_cache: dict[DoclingPipelineParams, Any] = {}


def _resolve_ocr_options(norm: DoclingPipelineParams) -> Any:
    """Build Docling OcrOptions for the requested backend, raising OcrBackendError on failure."""
    row, program = _installed_ocr_backend(norm.backend)
    pipeline_options_mod = importlib.import_module("docling.datamodel.pipeline_options")
    ocr_mode = pipeline_options_mod.OcrMode
    kwargs: dict[str, Any] = {
        "lang": list(norm.lang),
        "mode": ocr_mode.FULL_PAGE if norm.full_page_ocr else ocr_mode.DEFAULT,
    }
    if row.rapidocr_engine:
        kwargs["backend"] = row.rapidocr_engine
        kwargs["text_score"] = norm.text_score
        # RapidOcrOptions-only fields; other OcrOptions classes forbid extra fields.
        kwargs["model_size"] = norm.rapidocr_model_size
        kwargs["detect_orientation"] = norm.rapidocr_detect_orientation
    if program:
        # Absolute path, so Docling's --version/--list-langs/OCR subprocesses do not depend on the worker's PATH.
        kwargs["tesseract_cmd"] = program
    return getattr(pipeline_options_mod, row.options_class)(**kwargs)


# layout_model values with a Docling object-detection preset
# (docling/datamodel/stage_model_specs.py OBJECT_DETECTION_LAYOUT_<KEY>); any other value uses Heron.
_LAYOUT_KEYS = ("heron", "egret_large", "egret_xlarge")


def _resolve_layout_model_spec(layout_key: str) -> Any:
    """ObjectDetectionModelSpec for *layout_key*; keys outside _LAYOUT_KEYS use Heron."""
    stage_mod = importlib.import_module("docling.datamodel.stage_model_specs")
    key = layout_key if layout_key in _LAYOUT_KEYS else "heron"
    return getattr(stage_mod, "OBJECT_DETECTION_LAYOUT_" + key.upper()).model_spec


def _apply_pipeline_params(pipeline_options: Any, norm: DoclingPipelineParams) -> None:
    """Map normalized params onto Docling PdfPipelineOptions."""
    pipeline_options.images_scale = norm.images_scale
    pipeline_options.document_timeout = norm.document_timeout if norm.document_timeout > 0 else None

    if norm.artifacts_path:
        pipeline_options.artifacts_path = norm.artifacts_path

    pipeline_options.do_formula_enrichment = norm.do_formula_enrichment
    pipeline_options.do_code_enrichment = norm.do_code_enrichment

    acc_kwargs: dict[str, Any] = {}
    if norm.device:
        acc_kwargs["device"] = norm.device
    if norm.num_threads > 0:
        acc_kwargs["num_threads"] = norm.num_threads
    if acc_kwargs:
        # Constructing (not assigning fields) runs Docling's AcceleratorOptions.validate_device,
        # so a bad device raises ValueError (INVALID_PARAM).
        pipeline_options.accelerator_options = importlib.import_module(
            "docling.datamodel.pipeline_options"
        ).AcceleratorOptions(**acc_kwargs)

    table_opts = pipeline_options.table_structure_options
    if norm.table_mode == "fast":
        table_former = importlib.import_module("docling.datamodel.pipeline_options").TableFormerMode
        table_opts.mode = table_former.FAST
    table_opts.do_cell_matching = norm.do_cell_matching

    layout_opts = pipeline_options.layout_options
    layout_opts.create_orphan_clusters = norm.create_orphan_clusters
    layout_opts.model_spec = _resolve_layout_model_spec(norm.layout_model)


def _build_pipeline_options(norm: DoclingPipelineParams) -> Any:
    pipeline_options_mod = importlib.import_module("docling.datamodel.pipeline_options")
    pdf_opts_cls = pipeline_options_mod.PdfPipelineOptions

    ocr_options = _resolve_ocr_options(norm)

    pipeline_options = pdf_opts_cls(
        do_ocr=True,
        do_table_structure=True,
        # Kept so user-installed Docling OCR plugins still load; no ocr_backend row uses one.
        allow_external_plugins=True,
    )
    pipeline_options.ocr_options = ocr_options
    _apply_pipeline_params(pipeline_options, norm)
    return pipeline_options


def _get_docling_converter(norm: DoclingPipelineParams) -> Any:
    cached = _converter_cache.get(norm)
    if cached is not None:
        return cached

    _check_docling_version()
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
    _converter_cache.clear()
    _converter_cache[norm] = converter
    return converter


def _convert_image_bytes(image: Any, params: dict[str, Any]) -> Any:
    if not isinstance(image, (bytes, bytearray)):
        raise ValueError("image must be raw bytes")

    input_format = detect_vision_input_format(image, params)
    norm = DoclingPipelineParams.from_params(params, input_format=input_format)
    converter = _get_docling_converter(norm)
    base_models = importlib.import_module("docling.datamodel.base_models")
    buf = BytesIO(image)
    stream_name = "document.pdf" if input_format == "pdf" else "image.png"
    stream = base_models.DocumentStream(name=stream_name, stream=buf)
    result = converter.convert(stream)
    # Missing document after convert must raise a clear error, not None/AttributeError.
    document = getattr(result, "document", None)
    if document is None:
        raise RuntimeError("Docling conversion returned no document")
    return document


def _page_height(document: Any, prov: Any) -> float | None:
    if isinstance(prov, list) and prov:
        prov = prov[0]
    raw_page_no = prov.get("page_no") if isinstance(prov, dict) else getattr(prov, "page_no", None)
    if raw_page_no is None:
        return None
    try:
        page_no = int(raw_page_no)
    except (TypeError, ValueError):
        return None
    pages = getattr(document, "pages", None)
    page = pages.get(page_no) if isinstance(pages, dict) else None
    size = page.get("size") if isinstance(page, dict) else getattr(page, "size", None)
    height = size.get("height") if isinstance(size, dict) else getattr(size, "height", None)
    if height is None:
        return None
    try:
        return float(height)
    except (TypeError, ValueError):
        return None


def _is_bottom_left_bbox(bbox: Any) -> bool:
    origin = bbox.get("coord_origin") if isinstance(bbox, dict) else getattr(bbox, "coord_origin", None)
    if origin is None:
        return False
    return str(getattr(origin, "name", origin)).strip().upper() == "BOTTOMLEFT"


def prov_bbox_to_xywh(prov: Any, *, page_height: float | None = None) -> list[int]:
    """Convert provenance bbox to [x, y, w, h], flipping bottom-left coordinates when page_height is given."""
    if isinstance(prov, list) and prov:
        prov = prov[0]
    bbox = prov.get("bbox") if isinstance(prov, dict) else getattr(prov, "bbox", None)
    if bbox is None:
        bbox = prov.get("box") if isinstance(prov, dict) else None
    if bbox is None:
        return [0, 0, 0, 0]
    if page_height is None and _is_bottom_left_bbox(bbox):
        return [0, 0, 0, 0]
    return bbox_to_xywh(bbox, page_height=page_height)


def _map_docling_document(
    document: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Export document dict once and map text regions, structure blocks, tables, and text parts."""
    doc_dict: dict[str, Any] = {}
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
        box = prov_bbox_to_xywh(item.get("prov"), page_height=_page_height(document, item.get("prov")))
        confidence = float(item.get("confidence") or item.get("score") or 0.0)
        regions.append({"box": box, "text": text, "confidence": confidence})
        label = str(item.get("label") or item.get("type") or "text").strip().lower()
        blocks.append({"type": label or "text", "text": text, "box": box})
        text_parts.append(text)

    # Map tables
    tables: list[dict[str, Any]] = []
    live_tables = getattr(document, "tables", None)
    table_items = live_tables if (isinstance(live_tables, list) and live_tables) else (doc_dict.get("tables") or [])
    for table_index, item in enumerate(table_items, start=1):
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

        # TableItem has no export_to_dict; that call failed and boxes became
        # [0, 0, 0, 0]. Read coordinates from item.prov.
        prov = getattr(item, "prov", None) if not isinstance(item, dict) else item.get("prov")
        if prov:
            box = prov_bbox_to_xywh(prov, page_height=_page_height(document, prov))

        if table:
            tables.append(table)
            table_lines = table_to_tsv_lines(table)
            text_parts.extend(table_lines)
            blocks.append({"type": "table", "text": "\n".join(table_lines), "box": box})

    if not text_parts and hasattr(document, "export_to_markdown"):
        try:
            md = str(document.export_to_markdown() or "").strip()
            if md:
                text_parts.append(md)
                if not blocks:
                    blocks.append({"type": "text", "text": md, "box": [0, 0, 0, 0]})
        except Exception:
            log.debug("export_to_markdown failed for structure", exc_info=True)

    return regions, blocks, tables, text_parts


def _metrics_base(params: dict[str, Any], image: Any) -> dict[str, Any]:
    return {"engine": "docling", "ocr_backend": resolve_ocr_backend(params), "input_format": detect_vision_input_format(image, params)}


def export_docling_to_html(document: Any) -> str:
    """Return rich HTML from a DoclingDocument (bold, tables, headings), css-inlined for LO."""

    if hasattr(document, "export_to_html"):
        docling_doc_mod = importlib.import_module("docling_core.types.doc")
        image_ref_mode = docling_doc_mod.ImageRefMode
        # PLACEHOLDER: do not embed source/figure PNGs back into Writer (EMBEDDED
        # dumps data:image payloads that StarWriter inserts as extra graphics).
        # formula_to_mathml=True (Docling default): FormulaItem LaTeX → <math>
        # so Writer can create native Math objects. Leftover $$…$$ is converted
        # later in prepare_html_for_lo_import.
        raw = str(
            document.export_to_html(
                image_mode=image_ref_mode.PLACEHOLDER,
                formula_to_mathml=True,
                split_page_view=False,
            )
            or ""
        )
        return prepare_html_for_lo_import(raw)
    log.warning("Docling document %r lacks export_to_html; returning empty HTML", type(document).__name__)
    return ""


def _docling_error(exc: Exception, helper: str) -> dict[str, Any]:
    # OcrBackendError subclasses ValueError, so it is matched before the INVALID_PARAM arm.
    if isinstance(exc, ImportError):
        if is_css_inline_import_error(exc):
            return css_inline_unavailable_result(helper)
        root: BaseException = exc
        while root.__cause__ is not None:
            root = root.__cause__
        root_msg = str(root)
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
    if isinstance(exc, OcrBackendError):
        return error_result("OCR_BACKEND_UNAVAILABLE", str(exc), helper=helper)
    if isinstance(exc, DoclingApiMismatchError):
        return error_result("DOCLING_API_MISMATCH", str(exc), helper=helper)
    # Docling raises docling.exceptions.OcrLanguageNotSupportedError (a RuntimeError) when the engine
    # has no model for a requested language; its message lists the supported iso: tags. Matched by name so
    # this module never imports Docling just to map an error; getattr keeps pyright happy on Exception.
    if type(exc).__name__ == "OcrLanguageNotSupportedError":
        return error_result(
            "OCR_LANGUAGE_UNSUPPORTED",
            str(exc),
            helper=helper,
            details={"language": getattr(exc, "language", ""), "engine": getattr(exc, "engine", "")},
        )
    # Docling's TesseractOcrCliModel.__init__ (models/stages/ocr/tesseract_ocr_cli_model.py) wraps a failed
    # `tesseract --version` / `--list-langs` in this RuntimeError: the program was found but does not run.
    if isinstance(exc, RuntimeError) and str(exc).startswith("Tesseract is not available"):
        return error_result("OCR_BACKEND_UNAVAILABLE", str(exc), helper=helper)
    # RapidOCR has no PP-OCRv6 checkpoint at the chosen rapidocr_model_size for this language (tiny has no
    # Japanese model). Docling raises docling.exceptions.RapidOcrModelSizeNotSupportedError, a RuntimeError,
    # from RapidOcrModel.__init__ (_validate_rapidocr_model_size); it is a bad setting, so report INVALID_PARAM.
    if type(exc).__name__ == "RapidOcrModelSizeNotSupportedError":
        return error_result("INVALID_PARAM", str(exc), helper=helper)
    if isinstance(exc, ValueError):
        return error_result("INVALID_PARAM", str(exc), helper=helper)
    log.exception("Docling %s failed", helper)
    return error_result("VISION_ERROR", str(exc), helper=helper)


def _run_docling_conversion(
    image: Any,
    params: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    document = _convert_image_bytes(image, params)
    html = export_docling_to_html(document)
    regions, blocks, tables, text_parts = _map_docling_document(document)
    return html, regions, blocks, tables, text_parts


def extract_text(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_text"
    try:
        html, regions, _blocks, tables, text_parts = _run_docling_conversion(image, params)
    except Exception as exc:
        return _docling_error(exc, helper)

    # full_text includes every text part, tables included. Docling has no
    # mean_confidence, so that field is omitted.
    full_text = "\n".join(text_parts)
    warnings: list[str] = []
    if not full_text:
        warnings.append("No text detected.")

    # No regions: count full_text lines ("".splitlines() is empty).
    metrics = _metrics_base(params, image)
    metrics.update({"line_count": len(regions) or len(full_text.splitlines()), "table_count": len(tables)})

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
    try:
        html, _regions, blocks, tables, text_parts = _run_docling_conversion(image, params)
    except Exception as exc:
        return _docling_error(exc, helper)

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


def load_models(params: dict[str, Any]) -> dict[str, Any]:
    """Eagerly load Docling pipeline models into the cached converter for *params*.

    ``DocumentConverter`` is lazy; ``initialize_pipeline`` (docling
    ``document_converter.py:675``) loads the layout, table, and OCR models
    into the cached converter so the next OCR call reuses them.
    """
    try:
        fmt = detect_vision_input_format(None, params)
        norm = DoclingPipelineParams.from_params(params, input_format=fmt)
        converter = _get_docling_converter(norm)
        input_format_enum = importlib.import_module("docling.datamodel.base_models").InputFormat
        converter.initialize_pipeline(input_format_enum.PDF if fmt == "pdf" else input_format_enum.IMAGE)
    except Exception as exc:
        return _docling_error(exc, LOAD_MODELS_HELPER)
    return ok_result(LOAD_MODELS_HELPER)
