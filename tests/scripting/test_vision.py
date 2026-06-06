# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted vision helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting import vision as vision_mod
from plugin.scripting.vision import run_vision


@pytest.fixture(autouse=True)
def _reset_paddle_singleton():
    vision_mod._paddle_ocr_engine = None
    vision_mod._paddle_ocr_lang = None
    yield
    vision_mod._paddle_ocr_engine = None
    vision_mod._paddle_ocr_lang = None


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


@patch("plugin.scripting.vision._decode_image_bytes")
@patch("plugin.scripting.vision._get_paddle_ocr")
def test_extract_text_maps_regions_and_metrics(mock_get_engine, mock_decode):
    engine = MagicMock()
    engine.ocr.return_value = [_sample_ocr_page()]
    mock_get_engine.return_value = engine
    mock_decode.return_value = MagicMock()

    result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {"source": "selection"})

    assert result["status"] == "ok"
    assert result["helper"] == "extract_text"
    assert result["full_text"] == "Hello\nWorld"
    assert len(result["regions"]) == 2
    assert result["regions"][0]["box"] == [10, 10, 40, 20]
    assert result["regions"][0]["text"] == "Hello"
    assert result["regions"][0]["confidence"] == pytest.approx(0.98)
    assert result["metrics"]["line_count"] == 2
    assert result["metrics"]["mean_confidence"] == pytest.approx(0.945)
    assert result["warnings"] == []


@patch("plugin.scripting.vision._decode_image_bytes")
@patch("plugin.scripting.vision._get_paddle_ocr")
def test_extract_text_empty_ocr_adds_warning(mock_get_engine, mock_decode):
    engine = MagicMock()
    engine.ocr.return_value = [[]]
    mock_get_engine.return_value = engine
    mock_decode.return_value = MagicMock()

    result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {})

    assert result["status"] == "ok"
    assert result["full_text"] == ""
    assert result["warnings"] == ["No text detected."]


@patch("plugin.scripting.vision._get_paddle_ocr")
def test_extract_text_paddle_unavailable(mock_get_engine):
    mock_get_engine.side_effect = ImportError("paddleocr is not installed")

    result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {})

    assert result["status"] == "error"
    assert result["code"] == "PADDLEOCR_UNAVAILABLE"
    assert result["helper"] == "extract_text"


def test_unknown_helper_name():
    result = run_vision({"helper": "not_a_helper", "params": {}}, b"x", {})
    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_HELPER"


def test_unimplemented_helper_in_registry():
    result = run_vision({"helper": "detect_objects", "params": {}}, b"x", {})
    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_HELPER"
    assert "not implemented" in result["message"].lower()


@patch("plugin.scripting.vision._decode_image_bytes")
@patch("plugin.scripting.vision._get_paddle_ocr")
def test_extract_text_runtime_error_returns_vision_error(mock_get_engine, mock_decode):
    engine = MagicMock()
    engine.ocr.side_effect = RuntimeError("model failed")
    mock_get_engine.return_value = engine
    mock_decode.return_value = MagicMock()

    result = run_vision({"helper": "extract_text", "params": {}}, b"png-bytes", {})

    assert result["status"] == "error"
    assert result["code"] == "VISION_ERROR"
    assert "model failed" in result["message"]


@patch("plugin.framework.client.vision_client.run_code_in_user_venv")
def test_vision_client_passes_payload(mock_venv):
    from plugin.framework.client.vision_client import run_vision as run_trusted_vision

    ctx = MagicMock()
    mock_venv.return_value = {
        "status": "ok",
        "result": {"status": "ok", "helper": "extract_text", "full_text": "ok"},
    }

    result = run_trusted_vision(
        ctx,
        {"helper": "extract_text", "params": {}},
        b"png",
        context={"source": "selection"},
    )

    assert result["full_text"] == "ok"
    mock_venv.assert_called_once()
    _args, kwargs = mock_venv.call_args
    assert kwargs["session_id"] == "writeragent:vision"
    assert kwargs["data"]["spec"] == {"helper": "extract_text", "params": {}}
    assert kwargs["data"]["image"] == b"png"
    assert kwargs["data"]["context"] == {"source": "selection"}
