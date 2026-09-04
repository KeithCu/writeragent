# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Docling vision backend adapter."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from plugin.vision.venv import vision_docling as docling_mod
from plugin.vision.venv.vision_docling import extract_structure, extract_text


def _require_installed_od_docling() -> None:
    """Skip unless a real Docling install uses the ≥2.118 OD layout path.

    Does not mock layout types. Does not exercise the removable
    LayoutModelConfig / legacy LayoutOptions branch.
    """
    pytest.importorskip("docling.document_converter")
    pipeline_mod = pytest.importorskip("docling.datamodel.pipeline_options")
    default_opts = pipeline_mod.PdfPipelineOptions()
    if not docling_mod._is_object_detection_layout(default_opts.layout_options):
        pytest.skip(
            "installed Docling still defaults to legacy LayoutOptions; "
            "not testing that removable LayoutModelConfig branch"
        )


@pytest.fixture(autouse=True)
def _reset_converter_cache():
    docling_mod._converter_cache.clear()
    yield
    docling_mod._converter_cache.clear()


def _mock_document(*, texts=None, tables=None, markdown=None):
    doc = MagicMock()
    doc.export_to_dict.return_value = {
        "texts": texts
        if texts is not None
        else [
            {"text": "Line A", "prov": [{"bbox": {"l": 0, "t": 0, "r": 40, "b": 10}}], "score": 0.9},
        ],
        "tables": tables if tables is not None else [],
    }
    doc.export_to_markdown.return_value = "Line A" if markdown is None else markdown
    doc.export_to_html.return_value = "<p><strong>Line A</strong></p>" if markdown is None else f"<p>{markdown}</p>"
    return doc


@patch(
    "plugin.vision.venv.vision_html_export.export_docling_to_html",
    return_value="<p><strong>Line A</strong></p>",
)
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_text_maps_docling_document(mock_convert, _mock_html):
    mock_convert.return_value = _mock_document()

    result = extract_text(b"png", {"ocr_backend": "rapidocr_paddle", "lang": "en"})

    assert result["status"] == "ok"
    assert result["full_text"] == "Line A"
    assert "<strong>Line A</strong>" in result["html"]
    assert result["regions"][0]["text"] == "Line A"
    assert result["regions"][0]["box"] == [0, 0, 40, 10]
    assert result["metrics"]["engine"] == "docling"


@patch(
    "plugin.vision.venv.vision_html_export.export_docling_to_html",
    return_value="<h2>Title</h2><table></table>",
)
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_structure_maps_tables(mock_convert, _mock_html):
    mock_convert.return_value = _mock_document(
        texts=[{"text": "Title", "label": "section_header", "prov": []}],
        tables=[{"prov": [], "data": {"grid": [["A", "B"], ["1", "2"]]}}],
    )

    result = extract_structure(b"png", {"ocr_backend": "rapidocr_paddle"})

    assert result["status"] == "ok"
    assert result["tables"][0]["columns"] == ["A", "B"]
    assert result["tables"][0]["rows"] == [["1", "2"]]
    assert result["metrics"]["table_count"] == 1


def test_extract_structure_maps_table_cell_spans():
    mock_convert_doc = _mock_document(
        texts=[],
        tables=[
            {
                "prov": [],
                "data": {
                    "num_rows": 2,
                    "num_cols": 3,
                    "table_cells": [
                        {
                            "text": "ASSETS:",
                            "start_row_offset_idx": 0,
                            "start_col_offset_idx": 0,
                            "row_span": 1,
                            "col_span": 3,
                        },
                        {
                            "text": "Cash",
                            "start_row_offset_idx": 1,
                            "start_col_offset_idx": 0,
                            "row_span": 1,
                            "col_span": 1,
                        },
                        {
                            "text": "10",
                            "start_row_offset_idx": 1,
                            "start_col_offset_idx": 1,
                            "row_span": 1,
                            "col_span": 1,
                        },
                        {
                            "text": "11",
                            "start_row_offset_idx": 1,
                            "start_col_offset_idx": 2,
                            "row_span": 1,
                            "col_span": 1,
                        },
                    ],
                },
            }
        ],
    )
    with patch(
        "plugin.vision.venv.vision_html_export.export_docling_to_html",
        return_value="<table></table>",
    ), patch("plugin.vision.venv.vision_docling._convert_image_bytes", return_value=mock_convert_doc):
        result = extract_structure(b"png", {})

    table = result["tables"][0]
    assert table["columns"] == ["ASSETS:", "", ""]
    assert table["rows"] == [["Cash", "10", "11"]]
    assert table["spans"] == [{"row": 0, "col": 0, "rowspan": 1, "colspan": 3}]
    assert table["columns"].count("ASSETS:") == 1


def test_want_full_page_ocr_skips_pdfs():
    assert docling_mod._want_full_page_ocr({}, input_format="image") is True
    assert docling_mod._want_full_page_ocr({}, input_format="pdf") is False
    assert docling_mod._want_full_page_ocr({"ocr_mode": "full_page"}, input_format="pdf") is True
    assert docling_mod._want_full_page_ocr({"force_full_page_ocr": True}, input_format="pdf") is False


def test_extract_text_docling_missing():
    with patch("plugin.vision.venv.vision_docling._convert_image_bytes", side_effect=ImportError("docling is not installed")):
        result = extract_text(b"png", {})

    assert result["status"] == "error"
    assert result["code"] == "DOCLING_UNAVAILABLE"
    assert "pip install docling" in result["message"]


def test_extract_text_unknown_ocr_backend():
    with patch(
        "plugin.vision.venv.vision_docling._convert_image_bytes",
        side_effect=ValueError("Unknown ocr_backend 'nope'"),
    ):
        result = extract_text(b"png", {"ocr_backend": "nope"})

    assert result["status"] == "error"
    assert result["code"] == "OCR_BACKEND_UNAVAILABLE"


def test_apply_pipeline_params_maps_flat_keys():
    pipeline = MagicMock()
    table_opts = MagicMock()
    layout_opts = MagicMock()
    acc_opts = MagicMock()
    pipeline.table_structure_options = table_opts
    pipeline.layout_options = layout_opts
    pipeline.accelerator_options = acc_opts

    with patch("plugin.vision.venv.vision_docling._resolve_layout_model_spec", return_value="heron-spec"):
        fast_mode = MagicMock()
        mock_pipeline_mod = MagicMock()
        mock_pipeline_mod.TableFormerMode.FAST = fast_mode
        with patch("importlib.import_module", return_value=mock_pipeline_mod):
            docling_mod._apply_pipeline_params(
            pipeline,
            {
                "images_scale": 2.0,
                "document_timeout": 120,
                "device": "cpu",
                "num_threads": 8,
                "table_mode": "fast",
                "do_cell_matching": False,
                "create_orphan_clusters": False,
                "layout_model": "heron",
            },
            for_structure=True,
        )

    assert pipeline.images_scale == 2.0
    assert pipeline.document_timeout == 120
    assert acc_opts.device == "cpu"
    assert acc_opts.num_threads == 8
    assert table_opts.mode == fast_mode
    assert table_opts.do_cell_matching is False
    assert layout_opts.create_orphan_clusters is False
    assert layout_opts.model_spec == "heron-spec"


def test_resolve_ocr_options_surya():
    mock_surya = MagicMock()
    mock_surya.SuryaOcrOptions.return_value = "surya-opts"
    with patch("importlib.import_module", side_effect=lambda name: mock_surya if name == "docling_surya" else MagicMock()):
        opts = docling_mod._resolve_ocr_options({"ocr_backend": "surya", "lang": "en"})
    assert opts == "surya-opts"
    mock_surya.SuryaOcrOptions.assert_called_once_with(lang=["en"])


def test_build_pipeline_options_surya():
    mock_surya = MagicMock()
    mock_surya.SuryaOcrOptions.return_value = "surya-opts"

    mock_pipeline_mod = MagicMock()
    pdf_opts_cls = MagicMock()
    pdf_opts_instance = MagicMock()
    pdf_opts_cls.return_value = pdf_opts_instance
    mock_pipeline_mod.PdfPipelineOptions = pdf_opts_cls

    def side_effect(name):
        if name == "docling_surya":
            return mock_surya
        if name == "docling.datamodel.pipeline_options":
            return mock_pipeline_mod
        return MagicMock()

    with patch("importlib.import_module", side_effect=side_effect):
        pipeline_opts = docling_mod._build_pipeline_options({"ocr_backend": "surya"}, for_structure=True)

    assert pipeline_opts == pdf_opts_instance
    pdf_opts_cls.assert_called_once_with(
        do_ocr=True,
        do_table_structure=True,
        allow_external_plugins=True,
    )
    assert pdf_opts_instance.ocr_options == "surya-opts"
    assert pdf_opts_instance.ocr_model == "suryaocr"


def test_build_pipeline_options_real_docling_layout_has_get_engine_config():
    """Requires installed Docling ≥2.118; skips otherwise. No importlib mock.

    Catches issue 587 on a real install: OD layout model_spec must expose
    get_engine_config after WriterAgent builds pipeline options.
    """
    _require_installed_od_docling()
    opts = docling_mod._build_pipeline_options(
        {"ocr_backend": "rapidocr", "layout_model": "heron"},
        for_structure=True,
    )
    spec = opts.layout_options.model_spec
    assert hasattr(spec, "get_engine_config")
    assert callable(spec.get_engine_config)


def _tiny_hello_ocr_png() -> bytes:
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    img = Image.new("RGB", (240, 64), "white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 20), "Hello OCR", fill=(0, 0, 0))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _is_layout_api_error(result: dict) -> bool:
    message = str(result.get("message") or "")
    return "get_engine_config" in message or "LayoutModelConfig" in message


def _is_missing_weight_or_network_error(result: dict) -> bool:
    """Convert can need a first-time model download; skip rather than fail CI."""
    blob = " ".join(
        str(result.get(key) or "") for key in ("message", "code", "details")
    ).lower()
    markers = (
        "failed to download",
        "huggingface",
        "hf_hub",
        "connection",
        "timed out",
        "timeout",
        "offline",
        "no such file",
        "filenotfound",
        "model not found",
        "could not find",
        "unreachable",
        "max retries",
        "temporary failure",
    )
    return any(marker in blob for marker in markers)


@pytest.mark.timeout(300)
def test_extract_text_real_docling_tiny_png():
    """Requires installed Docling + PIL + rapidocr + onnxruntime; skips otherwise.

    Real extract_text (no Docling mocks) on a tiny in-memory PNG with
    layout_model=heron. Must not fail with get_engine_config / LayoutModelConfig
    (issue 587). First model download can be slow; image stays tiny.
    """
    _require_installed_od_docling()
    pytest.importorskip("PIL.Image")
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("css_inline")

    result = extract_text(
        _tiny_hello_ocr_png(),
        {"layout_model": "heron", "ocr_backend": "rapidocr", "lang": "en"},
    )

    assert not _is_layout_api_error(result), result
    if result.get("status") != "ok" and _is_missing_weight_or_network_error(result):
        pytest.skip(f"Docling convert needs weights/network: {result.get('message')}")

    assert result["status"] == "ok"
    assert isinstance(result.get("full_text"), str)
    assert isinstance(result.get("regions"), list)
    assert result.get("metrics", {}).get("engine") == "docling"


@pytest.mark.timeout(300)
def test_extract_structure_real_docling_pdf_magic():
    """Vector PDF bytes must use the PDF backend (not IMAGE OCR)."""
    from pathlib import Path

    _require_installed_od_docling()
    pytest.importorskip("css_inline")
    pdf_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "ocr_verification_corpus"
        / "01_sec_10k_apple_balance_sheet_page34.pdf"
    )
    if not pdf_path.is_file():
        pytest.skip(f"corpus PDF missing: {pdf_path}")

    result = extract_structure(
        pdf_path.read_bytes(),
        {"layout_model": "heron", "ocr_backend": "rapidocr", "lang": "en", "format": "auto"},
    )
    assert not _is_layout_api_error(result), result
    if result.get("status") != "ok" and _is_missing_weight_or_network_error(result):
        pytest.skip(f"Docling convert needs weights/network: {result.get('message')}")

    assert result["status"] == "ok"
    assert result.get("metrics", {}).get("input_format") == "pdf"
    assert result.get("metrics", {}).get("table_count", 0) >= 1
    table = (result.get("tables") or [{}])[0]
    assert any(span.get("colspan", 1) > 1 or span.get("rowspan", 1) > 1 for span in (table.get("spans") or [])), table
    joined = " ".join(str(c) for c in (table.get("columns") or []))
    assert joined.count("ASSETS:") <= 1

