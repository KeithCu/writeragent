# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted vision helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.vision.venv import vision_docling as docling_mod
from plugin.vision.venv import vision_paddle as paddle_mod
from plugin.vision.venv.vision import run_vision


@pytest.fixture(autouse=True)
def _reset_backend_singletons():
    paddle_mod._paddle_ocr_engine = None
    paddle_mod._paddle_ocr_lang = None
    paddle_mod._pp_structure_engine = None
    docling_mod._converter_cache.clear()
    yield
    paddle_mod._paddle_ocr_engine = None
    paddle_mod._paddle_ocr_lang = None
    paddle_mod._pp_structure_engine = None
    docling_mod._converter_cache.clear()


def _sample_ocr_page():
    return [
        [
            [[10, 10], [50, 10], [50, 30], [10, 30]],
            ("Hello", 0.98),
        ],
        [
            [[10, 40], [60, 40], [60, 60], [10, 60]],
            ("World", 0.91),
        ],
    ]


def _mock_docling_document(*, texts=None, tables=None, markdown=None):
    doc = MagicMock()
    doc.export_to_dict.return_value = {
        "texts": texts
        if texts is not None
        else [
            {"text": "Hello", "prov": [{"bbox": {"l": 10, "t": 10, "r": 50, "b": 30}}], "confidence": 0.98},
            {"text": "World", "prov": [{"bbox": {"l": 10, "t": 40, "r": 60, "b": 60}}], "confidence": 0.91},
        ],
        "tables": tables if tables is not None else [],
    }
    doc.export_to_markdown.return_value = "Hello\nWorld" if markdown is None else markdown
    doc.export_to_html.return_value = "<p>Hello</p><p>World</p>" if markdown is None else f"<p>{markdown}</p>"
    return doc


@patch("plugin.vision.venv.vision_html_export.export_docling_to_html", return_value="<p>Hello</p><p>World</p>")
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_text_docling_default_maps_regions(mock_convert, _mock_html):
    mock_convert.return_value = _mock_docling_document()

    result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {"source": "selection"})

    assert result["status"] == "ok"
    assert result["helper"] == "extract_text"
    assert result["full_text"] == "Hello\nWorld"
    assert "<p>Hello</p>" in result["html"]
    assert len(result["regions"]) == 2
    assert result["regions"][0]["box"] == [10, 10, 40, 20]
    assert result["metrics"]["engine"] == "docling"
    assert result["metrics"]["ocr_backend"] == "rapidocr"


@patch("plugin.vision.venv.vision_html_export.export_docling_to_html", return_value="")
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_text_docling_empty_adds_warning(mock_convert, _mock_html):
    mock_convert.return_value = _mock_docling_document(texts=[], markdown="")

    result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {})

    assert result["status"] == "ok"
    assert result["full_text"] == ""
    assert result["warnings"] == ["No text detected."]


@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_text_docling_unavailable_falls_back_to_paddle(mock_convert):
    mock_convert.side_effect = ImportError("docling is not installed")

    with patch("plugin.vision.venv.vision_paddle._decode_image_bytes") as mock_decode, patch(
        "plugin.vision.venv.vision_paddle._get_paddle_ocr"
    ) as mock_get_engine:
        engine = MagicMock()
        engine.ocr.return_value = [_sample_ocr_page()]
        mock_get_engine.return_value = engine
        mock_decode.return_value = MagicMock()

        result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {})

    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"
    assert "Hello</p>" in result["html"]
    assert "World</p>" in result["html"]
    assert "Docling unavailable; fell back to PaddleOCR." in result["warnings"]
    assert result["metrics"]["fallback_from"] == "docling"


@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_text_docling_unavailable_no_fallback(mock_convert):
    mock_convert.side_effect = ImportError("docling is not installed")

    result = run_vision(
        {"helper": "extract_text", "params": {"fallback_engine": False}},
        b"png-bytes",
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "DOCLING_UNAVAILABLE"


@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_text_docling_api_error_falls_back_to_paddle(mock_convert):
    mock_convert.side_effect = AttributeError(
        "'LayoutModelConfig' object has no attribute 'get_engine_config'"
    )

    with patch("plugin.vision.venv.vision_paddle._decode_image_bytes") as mock_decode, patch(
        "plugin.vision.venv.vision_paddle._get_paddle_ocr"
    ) as mock_get_engine:
        engine = MagicMock()
        engine.ocr.return_value = [_sample_ocr_page()]
        mock_get_engine.return_value = engine
        mock_decode.return_value = MagicMock()

        result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {})

    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"
    assert "Docling layout API error; fell back to PaddleOCR." in result["warnings"]
    assert result["metrics"]["fallback_from"] == "docling"


@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_text_docling_unrelated_vision_error_does_not_fallback(mock_convert):
    mock_convert.side_effect = RuntimeError("model failed")

    result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {})

    assert result["status"] == "error"
    assert result["code"] == "VISION_ERROR"
    assert "model failed" in result["message"]


@patch("plugin.vision.venv.vision_paddle._decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_paddle_ocr")
def test_extract_text_paddle_engine_maps_regions(mock_get_engine, mock_decode):
    engine = MagicMock()
    engine.ocr.return_value = [_sample_ocr_page()]
    mock_get_engine.return_value = engine
    mock_decode.return_value = MagicMock()

    result = run_vision({"helper": "extract_text", "params": {"engine": "paddle"}}, b"png-bytes", {})

    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"
    assert "Hello</p>" in result["html"]
    assert "World</p>" in result["html"]
    assert result["metrics"]["engine"] == "paddle"
    assert len(result["regions"]) == 2


@patch("plugin.vision.venv.vision_paddle._get_paddle_ocr")
def test_extract_text_paddle_unavailable(mock_get_engine):
    mock_get_engine.side_effect = ImportError("paddleocr is not installed")

    result = run_vision({"helper": "extract_text", "params": {"engine": "paddle"}}, b"png-bytes", {})

    assert result["status"] == "error"
    assert result["code"] == "PADDLEOCR_UNAVAILABLE"
    assert "pip install paddleocr paddlepaddle numpy" in result["message"]


def test_unknown_helper_name():
    result = run_vision({"helper": "not_a_helper", "params": {}}, b"x", {})
    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_HELPER"


def test_unimplemented_helper_in_registry():
    result = run_vision({"helper": "detect_objects", "params": {}}, b"x", {})
    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_HELPER"
    assert "not implemented" in result["message"].lower()


def _sample_structure_page():
    return [
        {"type": "text", "bbox": [10, 10, 100, 30], "res": [{"text": "Invoice"}]},
        {
            "type": "table",
            "bbox": [10, 40, 200, 120],
            "res": {
                "html": "<table><tr><th>Item</th><th>Qty</th></tr><tr><td>Widget</td><td>2</td></tr></table>",
            },
        },
    ]


@patch("plugin.vision.venv.vision_html_export.export_docling_to_html", return_value="<table></table>")
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_structure_docling_default(mock_convert, _mock_html):
    mock_convert.return_value = _mock_docling_document(
        texts=[{"text": "Invoice", "label": "text", "prov": [{"bbox": {"l": 10, "t": 10, "r": 100, "b": 30}}]}],
        tables=[
            {
                "prov": [{"bbox": {"l": 10, "t": 40, "r": 200, "b": 120}}],
                "data": {"grid": [["Item", "Qty"], ["Widget", "2"]]},
            }
        ],
    )

    result = run_vision({"helper": "extract_structure", "params": {}}, b"png-bytes", {})

    assert result["status"] == "ok"
    assert result["helper"] == "extract_structure"
    assert "Invoice" in result["full_text"]
    assert result["metrics"]["block_count"] >= 1
    assert result["metrics"]["table_count"] == 1
    assert result["tables"][0]["columns"] == ["Item", "Qty"]
    assert result["tables"][0]["rows"] == [["Widget", "2"]]


@patch("plugin.vision.venv.vision_paddle._decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_pp_structure")
def test_extract_structure_paddle_engine(mock_get_engine, mock_decode):
    engine = MagicMock()
    engine.predict.return_value = [_sample_structure_page()]
    mock_get_engine.return_value = engine
    mock_decode.return_value = MagicMock()

    result = run_vision({"helper": "extract_structure", "params": {"engine": "paddle"}}, b"png-bytes", {})

    assert result["status"] == "ok"
    assert "Invoice" in result["full_text"]
    assert result["metrics"]["table_count"] == 1
    assert result["html"].lower().count("<table") == 1
    assert "&lt;table" not in result["html"].lower()
    table_blocks = [block for block in result["blocks"] if block.get("type") == "table"]
    assert table_blocks
    assert "<table" not in str(table_blocks[0].get("text") or "").lower()


def test_paddle_html_table_preserves_colspan_header():
    html = (
        "<table>"
        '<tr><th colspan="3">ASSETS:</th></tr>'
        "<tr><td>Cash</td><td>10</td><td>11</td></tr>"
        "</table>"
    )
    table = paddle_mod._table_from_structure_res({"html": html}, name="table_1")
    assert table is not None
    assert table["columns"] == ["ASSETS:", "", ""]
    assert table["rows"] == [["Cash", "10", "11"]]
    assert table["spans"] == [{"row": 0, "col": 0, "rowspan": 1, "colspan": 3}]
    assert table["columns"].count("ASSETS:") == 1
    from plugin.vision.venv.vision_html_export import _html_table_from_columns_rows

    assert 'colspan="3"' in _html_table_from_columns_rows(table["columns"], table["rows"], table["spans"])

    shifted = (
        "<table>"
        '<tr><th colspan="2">Group</th><th>Note</th></tr>'
        '<tr><td rowspan="2">A</td><td>B</td><td>C</td></tr>'
        "<tr><td>D</td><td>E</td></tr>"
        "</table>"
    )
    spanned = paddle_mod._table_from_structure_res({"html": shifted}, name="table_2")
    assert spanned is not None
    assert spanned["columns"] == ["Group", "", "Note"]
    assert spanned["rows"] == [["A", "B", "C"], ["", "D", "E"]]
    assert spanned["spans"] == [
        {"row": 0, "col": 0, "rowspan": 1, "colspan": 2},
        {"row": 1, "col": 0, "rowspan": 2, "colspan": 1},
    ]


def test_text_from_structure_res_drops_raw_table_html():
    html = "<table><tr><th>Item</th></tr><tr><td>Widget</td></tr></table>"
    assert paddle_mod._text_from_structure_res({"html": html}) == ""
    assert paddle_mod._text_from_structure_res({"text": "Caption", "html": html}) == "Caption"
    assert paddle_mod._text_from_structure_res({"html": "<p>Note</p>"}) == "<p>Note</p>"


@patch("plugin.vision.venv.vision_paddle._get_pp_structure")
def test_extract_structure_paddle_unavailable(mock_get_engine):
    mock_get_engine.side_effect = ImportError("PPStructureV3 is not available")

    result = run_vision({"helper": "extract_structure", "params": {"engine": "paddle"}}, b"png-bytes", {})

    assert result["status"] == "error"
    assert result["code"] == "PADDLEOCR_UNAVAILABLE"


@patch("plugin.vision.venv.vision_paddle._decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_paddle_ocr")
def test_extract_text_runtime_error_returns_vision_error(mock_get_engine, mock_decode):
    engine = MagicMock()
    engine.ocr.side_effect = RuntimeError("model failed")
    mock_get_engine.return_value = engine
    mock_decode.return_value = MagicMock()

    result = run_vision({"helper": "extract_text", "params": {"engine": "paddle"}}, b"png-bytes", {})

    assert result["status"] == "error"
    assert result["code"] == "VISION_ERROR"
    assert "model failed" in result["message"]


@patch("plugin.scripting.client._run_trusted_action")
def test_vision_client_passes_payload(mock_action):
    from plugin.scripting.client import run_vision as run_trusted_vision

    ctx = MagicMock()
    mock_action.return_value = {
        "status": "ok",
        "helper": "extract_text",
        "full_text": "ok",
    }

    result = run_trusted_vision(
        ctx,
        {"helper": "extract_text", "params": {}},
        b"png",
        context={"source": "selection"},
    )

    assert result["full_text"] == "ok"
    mock_action.assert_called_once()
    _args, kwargs = mock_action.call_args
    assert "session_id" not in kwargs
    assert kwargs["domain"] == "vision"
    assert kwargs["helper"] == "extract_text"
    assert kwargs["params"] == {}
    assert kwargs["context"] == {"source": "selection"}
    assert kwargs["additional_data"] == {"image": b"png"}


@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_pdf_docling_unavailable_does_not_call_paddle(mock_convert):
    mock_convert.side_effect = ImportError("docling is not installed")
    with patch("plugin.vision.venv.vision_paddle._decode_image_bytes") as decode, patch(
        "plugin.vision.venv.vision_paddle._get_paddle_ocr"
    ) as paddle:
        result = run_vision({"helper": "extract_text", "params": {}}, b"%PDF-1.4 not-a-png", {})
    decode.assert_not_called()
    paddle.assert_not_called()
    assert result["status"] == "error"
    assert result["code"] == "DOCLING_UNAVAILABLE"


def test_run_vision_rejects_oversized_image():
    with patch("plugin.vision.venv.vision.VISION_IMAGE_MAX_BYTES", 4):
        result = run_vision({"helper": "extract_text"}, b"12345", {})
    assert result["status"] == "error"
    assert result["code"] == "IMAGE_TOO_LARGE"
