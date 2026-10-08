# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared vision helper utilities."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from PIL import Image

from plugin.vision.vision_common import (
    bbox_to_xywh,
    css_inline_unavailable_result,
    decode_image_bytes,
    detect_vision_input_format,
    is_css_inline_import_error,
    merge_vision_params,
    resolve_engine,
    resolve_vision_insert_mode,
    table_to_tsv_lines,
)


def test_detect_vision_input_format_pdf_magic():
    assert detect_vision_input_format(b"%PDF-1.7\n%", {}) == "pdf"
    assert detect_vision_input_format(b"\x89PNG\r\n", {}) == "image"
    assert detect_vision_input_format(b"%PDF-1.4", {"format": "image"}) == "image"
    assert detect_vision_input_format(b"not-pdf", {"format": "pdf"}) == "pdf"
    assert detect_vision_input_format(b"%PDF", {"format": "auto"}) == "pdf"


def test_resolve_vision_insert_mode_template_overrides_config():
    ctx = object()
    assert resolve_vision_insert_mode(ctx, {"insert_mode": "structured"}) == "structured"


def test_resolve_vision_insert_mode_reads_config():
    ctx = object()
    with patch("plugin.framework.config.get_config", return_value="structured"):
        assert resolve_vision_insert_mode(ctx, None) == "structured"


def test_is_css_inline_import_error():
    exc = ImportError("No module named 'css_inline'")
    assert is_css_inline_import_error(exc) is True
    assert is_css_inline_import_error(ImportError("docling missing")) is False


def test_css_inline_unavailable_result():
    result = css_inline_unavailable_result("extract_text")
    assert result["status"] == "error"
    assert result["code"] == "CSS_INLINE_UNAVAILABLE"
    assert result["helper"] == "extract_text"
    assert "css-inline" in result["message"]


def test_merge_vision_params_logs_exception_and_drops_ctx_gate():
    # What was wrong: ctx is not None gate skipped Settings when ctx=None, and except Exception: pass swallowed errors.
    # Why this change: verify config failures are logged and config works with ctx=None.
    with patch("plugin.framework.config.get_config", side_effect=Exception("config read failed")), patch(
        "plugin.vision.vision_common.log.exception"
    ) as mock_log:
        merged = merge_vision_params(None, {"lang": "de"})
        assert merged["lang"] == "de"
        mock_log.assert_called_once()


def test_resolve_engine_valid_and_unknown():
    # What was wrong: resolve_engine accepted typos like "padle" and silently used docling.
    # Why this change: reject unknown engines with ValueError.
    assert resolve_engine({"engine": "docling"}) == "docling"
    assert resolve_engine({"engine": "paddle"}) == "paddle"
    with pytest.raises(ValueError, match="Unknown vision engine 'padle'"):
        resolve_engine({"engine": "padle"})


def test_decode_image_bytes_composites_transparent_image_onto_white():
    # What was wrong: convert("RGB") on transparent images converted transparent background to black.
    # Why this change: composite alpha channel onto white background before converting to RGB.
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))  # fully transparent
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    arr = decode_image_bytes(buf.getvalue())
    assert (arr == 255).all()


def test_table_to_tsv_lines_preserves_empty_cells():
    # What was wrong: if str(c) dropped empty cells and misaligned table columns.
    # Why this change: table_to_tsv_lines keeps empty cells.
    table = {
        "columns": ["Col1", "", "Col3"],
        "rows": [["A", "", "C"], ["D", "E", ""]],
    }
    lines = table_to_tsv_lines(table)
    assert lines == ["Col1\t\tCol3", "A\t\tC", "D\tE\t"]


def test_bbox_to_xywh_bottomleft_flip():
    # What was wrong: BOTTOMLEFT coord_origin with t > b yielded height 0 and unflipped y.
    # Why this change: flip y with page_height and compute positive height abs(t - b).
    docling_box = {"l": 10.0, "b": 20.0, "r": 50.0, "t": 60.0, "coord_origin": "BOTTOMLEFT"}
    box = bbox_to_xywh(docling_box, page_height=100.0)
    assert box == [10, 40, 40, 40]
