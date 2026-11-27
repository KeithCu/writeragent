# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Docling backend for trusted vision helpers — unified OCR/layout/table pipeline."""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from plugin.vision.venv.vision_html_export import prepare_html_for_lo_import
from plugin.vision.vision_common import (
    DOCLING_MIN_VERSION,
    DOCLING_REQUIREMENT,
    LOAD_MODELS_HELPER,
    OCR_BACKENDS,
    OcrBackend,
    bbox_to_xywh,
    css_inline_unavailable_result,
    detect_vision_input_format,
    docling_too_old,
    error_result,
    is_css_inline_import_error,
    ocr_module_package,
    ok_result,
    resolve_ocr_backend,
    table_from_span_cells,
    table_to_tsv_lines,
    vision_bool,
)

log = logging.getLogger(__name__)

# Double quotes stop the shell reading ">" as a redirect (cmd.exe and POSIX shells alike).
_DOCLING_INSTALL_CMD = f'pip install "{DOCLING_REQUIREMENT}" onnxruntime css-inline'

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


def _import_engine_module(entry: str) -> None:
    """Import one OcrBackend.modules entry ("module" or "module:attribute")."""
    module, _sep, attr = entry.partition(":")
    imported = importlib.import_module(module)
    if attr:
        getattr(imported, attr)


def _installed_ocr_backend(name: str) -> tuple[OcrBackend, str]:
    """Return (row, program) for *name*; raise OcrBackendError when unknown, on the wrong platform, or when its modules/program are missing or broken."""
    row = OCR_BACKENDS.get(name)
    if row is None:
        raise OcrBackendError(
            f"Unknown ocr_backend {name!r}; expected one of: {', '.join(sorted(OCR_BACKENDS))}"
        )
    if row.sys_platform and sys.platform != row.sys_platform:
        raise OcrBackendError(
            f"OCR backend {name!r} does not run on this system (the Python venv reports {sys.platform!r}). {row.install_hint}"
        )
    missing: list[str] = []
    broken: list[str] = []
    for entry in row.modules:
        top = ocr_module_package(entry)
        try:
            _import_engine_module(entry)
        except Exception as exc:
            if isinstance(exc, ModuleNotFoundError) and exc.name == top:
                missing.append(top)
            else:
                broken.append(f"{top} cannot be imported ({exc})")
    program = next((found for found in map(shutil.which, row.programs) if found), "")
    if row.programs and not program:
        missing.append(row.programs[0])
    if missing:
        raise OcrBackendError(
            f"OCR backend {name!r} needs {', '.join(missing)}, which is not installed for the Python venv. {row.install_hint}"
        )
    if broken:
        raise OcrBackendError(
            f"OCR backend {name!r} was found but cannot start: {'; '.join(broken)}. {row.install_hint}"
        )
    return row, program


class DoclingApiMismatchError(RuntimeError):
    """Installed Docling is older than DOCLING_MIN_VERSION; reported as DOCLING_API_MISMATCH."""


def _check_docling_version() -> None:
    """Raise DoclingApiMismatchError when installed Docling is older than DOCLING_MIN_VERSION.

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
        if docling_too_old(installed):
            raise DoclingApiMismatchError(
                f"Docling {installed} is installed, but WriterAgent Vision needs Docling "
                "%d.%d or newer. Upgrade it in the Settings → Python venv: " % DOCLING_MIN_VERSION
                + f'pip install -U "{DOCLING_REQUIREMENT}"'
            )
        return


_OCRMAC_RECOGNITION = ("accurate", "fast")  # OcrMacOptions.recognition values ocrmac accepts


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
    ocrmac_recognition: str         # "accurate" | "fast"; validated in from_params

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
        recognition = str(params.get("ocrmac_recognition") or "accurate").strip().lower() or "accurate"
        if recognition not in _OCRMAC_RECOGNITION:
            raise ValueError(f"ocrmac_recognition must be 'accurate' or 'fast', not {recognition!r}")
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
            ocrmac_recognition=recognition,
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
    if row.threshold_field:
        kwargs[row.threshold_field] = norm.text_score
    # Engine-only fields are added only for their own engine, because the other OcrOptions classes forbid extra fields.
    if row.rapidocr_engine:
        kwargs["backend"] = row.rapidocr_engine
        kwargs["model_size"] = norm.rapidocr_model_size
        kwargs["detect_orientation"] = norm.rapidocr_detect_orientation
    if norm.backend == "ocrmac":
        kwargs["recognition"] = norm.ocrmac_recognition
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
        if not Path(norm.artifacts_path).expanduser().is_dir():
            raise FileNotFoundError(f"artifacts_path {norm.artifacts_path!r} is not a folder.")
        pipeline_options.artifacts_path = norm.artifacts_path

    pipeline_options.do_formula_enrichment = norm.do_formula_enrichment
    pipeline_options.do_code_enrichment = norm.do_code_enrichment

    acc_kwargs: dict[str, Any] = {}
    if norm.device:
        acc_kwargs["device"] = norm.device
    if norm.num_threads > 0:
        acc_kwargs["num_threads"] = norm.num_threads
    table_opts = pipeline_options.table_structure_options
    if acc_kwargs or norm.table_mode == "fast":
        pipeline_options_mod = importlib.import_module("docling.datamodel.pipeline_options")
        if acc_kwargs:
            # Constructing (not assigning fields) runs Docling's AcceleratorOptions.validate_device,
            # so a bad device raises ValueError (INVALID_PARAM).
            pipeline_options.accelerator_options = pipeline_options_mod.AcceleratorOptions(**acc_kwargs)
        if norm.table_mode == "fast":
            table_opts.mode = pipeline_options_mod.TableFormerMode.FAST
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
            data = item.data
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

        # TableItem has no export_to_dict, so read coordinates from item.prov.
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
    # Docling's engine models raise these from __init__ when pre-flight found the engine but it cannot start:
    # NemotronOcrModel re-raises a failed engine import as a new ImportError inside the except block (the real
    # cause is __context__), TesseractOcrCliModel wraps a failing `tesseract --version`,
    # ocr_mac_model._get_vision_languages raises RuntimeError from the pyobjc error, and
    # NemotronOcrModel.validate_runtime raises RuntimeError when the venv's platform, Python, CUDA or device
    # does not suit Nemotron. Reporting them as OCR_BACKEND_UNAVAILABLE keeps them out of the
    # DOCLING_UNAVAILABLE arm, so no Paddle fallback runs.
    if isinstance(exc, (ImportError, RuntimeError)):
        backend = next(
            (
                bid
                for bid, row in OCR_BACKENDS.items()
                if row.startup_errors and str(exc).startswith(row.startup_errors)
            ),
            "",
        )
        if backend:
            cause = exc.__cause__ or exc.__context__ or exc
            if str(exc).startswith("Apple Vision did not report any recognition language"):
                detail = f"{exc} ({cause})" if cause is not exc else str(exc)
            else:
                detail = f"{str(cause).rstrip('.')}. {OCR_BACKENDS[backend].install_hint}"
            return error_result(
                "OCR_BACKEND_UNAVAILABLE",
                f"OCR backend {backend!r} was found but cannot start: {detail}",
                helper=helper,
            )
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
    # FileNotFoundError covers absent files; RapidOCR raises rapidocr.utils.download_file.DownloadFileException
    # (a plain Exception) when a first-use download fails, and it is matched by name so RapidOCR is not imported.
    if isinstance(exc, FileNotFoundError) or type(exc).__name__ == "DownloadFileException":
        return error_result(
            "OCR_MODELS_MISSING",
            f"Model files for OCR are missing: {exc}\nDownload them with docling-tools models download -o <folder>, or clear vision.artifacts_path and go online so they download on first use.",
            helper=helper,
        )
    # OcrBackendError subclasses ValueError, so it is matched before the INVALID_PARAM arm.
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
    # RapidOCR has no PP-OCRv6 checkpoint at the chosen rapidocr_model_size for this language (tiny has no
    # Japanese model). Docling raises docling.exceptions.RapidOcrModelSizeNotSupportedError, a RuntimeError,
    # from RapidOcrModel.__init__ (_validate_rapidocr_model_size); it is a bad setting, so report INVALID_PARAM.
    if type(exc).__name__ == "RapidOcrModelSizeNotSupportedError":
        return error_result("INVALID_PARAM", str(exc), helper=helper)
    # Docling raises docling.exceptions.AcceleratorDeviceNotAvailableError (a RuntimeError) from
    # docling/utils/accelerator_utils.py decide_device when the requested cuda/mps/xpu device is not in this
    # machine or PyTorch build. Docling's text suggests a command-line option, so name the setting to change.
    if type(exc).__name__ == "AcceleratorDeviceNotAvailableError":
        return error_result(
            "INVALID_PARAM",
            f"Docling cannot use the hardware set in vision.device: {exc}\nSet vision.device to auto, which uses a GPU only when Docling finds one.",
            helper=helper,
        )
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
