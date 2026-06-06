# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision helper result egress."""

from __future__ import annotations

import pytest

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.vision_egress import format_vision_for_writer, is_vision_result


def test_is_vision_result_ok():
    assert is_vision_result({"status": "ok", "helper": "extract_text", "full_text": "hi"})


def test_is_vision_result_error():
    assert is_vision_result({"status": "error", "code": "PADDLEOCR_UNAVAILABLE", "message": "missing"})


def test_is_vision_result_rejects_non_dict():
    assert not is_vision_result("text")
    assert not is_vision_result(None)


def test_is_vision_result_rejects_missing_status():
    assert not is_vision_result({"helper": "extract_text"})


def test_format_vision_for_writer_success():
    text = format_vision_for_writer({"status": "ok", "helper": "extract_text", "full_text": "line1\nline2"})
    assert text == "line1\nline2"


def test_format_vision_for_writer_empty_text():
    assert format_vision_for_writer({"status": "ok", "helper": "extract_text", "full_text": ""}) == ""


def test_format_vision_for_writer_error_raises():
    with pytest.raises(ToolExecutionError) as exc:
        format_vision_for_writer(
            {
                "status": "error",
                "code": "PADDLEOCR_UNAVAILABLE",
                "helper": "extract_text",
                "message": "Install paddleocr",
            }
        )
    assert exc.value.code == "PADDLEOCR_UNAVAILABLE"


def test_format_vision_for_writer_missing_full_text_raises():
    with pytest.raises(ToolExecutionError) as exc:
        format_vision_for_writer({"status": "ok", "helper": "extract_text"})
    assert exc.value.code == "VISION_ERROR"
