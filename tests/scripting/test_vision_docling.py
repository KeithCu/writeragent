# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Docling vision backend adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting import vision_docling as docling_mod
from plugin.scripting.vision_docling import extract_structure, extract_text


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
    return doc


@patch("plugin.scripting.vision_docling._convert_image_bytes")
def test_extract_text_maps_docling_document(mock_convert):
    mock_convert.return_value = _mock_document()

    result = extract_text(b"png", {"ocr_backend": "rapidocr_paddle", "lang": "en"})

    assert result["status"] == "ok"
    assert result["full_text"] == "Line A"
    assert result["regions"][0]["text"] == "Line A"
    assert result["regions"][0]["box"] == [0, 0, 40, 10]
    assert result["metrics"]["engine"] == "docling"


@patch("plugin.scripting.vision_docling._convert_image_bytes")
def test_extract_structure_maps_tables(mock_convert):
    mock_convert.return_value = _mock_document(
        texts=[{"text": "Title", "label": "section_header", "prov": []}],
        tables=[{"prov": [], "data": {"grid": [["A", "B"], ["1", "2"]]}}],
    )

    result = extract_structure(b"png", {"ocr_backend": "rapidocr_paddle"})

    assert result["status"] == "ok"
    assert result["tables"][0]["columns"] == ["A", "B"]
    assert result["tables"][0]["rows"] == [["1", "2"]]
    assert result["metrics"]["table_count"] == 1


def test_extract_text_docling_missing():
    with patch("plugin.scripting.vision_docling._convert_image_bytes", side_effect=ImportError("docling is not installed")):
        result = extract_text(b"png", {})

    assert result["status"] == "error"
    assert result["code"] == "DOCLING_UNAVAILABLE"
    assert "pip install docling" in result["message"]


def test_extract_text_unknown_ocr_backend():
    with patch(
        "plugin.scripting.vision_docling._convert_image_bytes",
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

    with patch("plugin.scripting.vision_docling._resolve_layout_model_spec", return_value="heron-spec"):
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
