# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared vision helper utilities."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.vision.vision_common import (
    bbox_to_xywh,
    css_inline_unavailable_result,
    detect_vision_input_format,
    is_css_inline_import_error,
    merge_vision_params,
    resolve_engine,
    resolve_vision_insert_mode,
    table_from_span_cells,
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


class _Box:
    def __init__(self, l, t, r, b, coord_origin=""):  # noqa: E741
        self.l, self.t, self.r, self.b = l, t, r, b
        self.coord_origin = coord_origin


class _NumpyRow:
    """A 4-number box that is not a list/tuple, like a numpy rec_boxes row."""

    def __init__(self, values):
        self._values = values

    def __len__(self):
        return len(self._values)

    def __getitem__(self, index):
        return self._values[index]


def test_bbox_to_xywh_sequence_forms():
    quad = [[10, 10], [40, 10], [40, 20], [10, 20]]
    assert bbox_to_xywh(quad) == [10, 10, 30, 10]
    assert bbox_to_xywh([10, 10, 300, 200]) == [10, 10, 290, 190]
    assert bbox_to_xywh(_NumpyRow([10, 10, 300, 200])) == [10, 10, 290, 190]
    assert bbox_to_xywh([10, 10, 10, 20]) == [10, 10, 0, 10]
    assert bbox_to_xywh([]) == [0, 0, 0, 0]
    assert bbox_to_xywh(None) == [0, 0, 0, 0]
    assert bbox_to_xywh([[10, 10]]) == [10, 10, 0, 0]
    assert bbox_to_xywh([[10], [20]]) == [0, 0, 0, 0]


def test_bbox_to_xywh_dict_and_object_forms():
    assert bbox_to_xywh({"l": 10, "t": 10, "r": 50, "b": 30}) == [10, 10, 40, 20]
    assert bbox_to_xywh(_Box(10, 10, 50, 30)) == [10, 10, 40, 20]
    bottomleft = {"l": 10.0, "b": 20.0, "r": 50.0, "t": 60.0, "coord_origin": "BOTTOMLEFT"}
    assert bbox_to_xywh(bottomleft, page_height=100.0) == [10, 40, 40, 40]
    assert bbox_to_xywh(_Box(10.0, 60.0, 50.0, 20.0, "BOTTOMLEFT"), page_height=100.0) == [10, 40, 40, 40]
    assert bbox_to_xywh({"l": "bad", "t": 1, "r": 2, "b": 3}) == [0, 0, 0, 0]


def test_bbox_to_xywh_malformed_returns_zeros():
    # Malformed input returns [0, 0, 0, 0], as the docstring says.
    assert bbox_to_xywh([0, None, 5, 5]) == [0, 0, 0, 0]
    assert bbox_to_xywh([0, float("nan"), 5, 5]) == [0, 0, 0, 0]
    assert bbox_to_xywh([0, float("inf"), 5, 5]) == [0, 0, 0, 0]
    assert bbox_to_xywh(_NumpyRow([10, None, 30, 40])) == [0, 0, 0, 0]
    assert bbox_to_xywh([[float("nan"), 1], [2, 2], [3, 3], [4, 4]]) == [0, 0, 0, 0]
    assert bbox_to_xywh([[float("inf"), 1.0], [2.0, 2.0], [3.0, 3.0], [1.0, 4.0]]) == [0, 0, 0, 0]
    assert bbox_to_xywh({"l": float("nan"), "t": 10.0, "r": 20.0, "b": 30.0}) == [0, 0, 0, 0]
    assert bbox_to_xywh({"l": "nan", "t": 0, "r": 10, "b": 10}) == [0, 0, 0, 0]


def test_table_from_span_cells_falls_through_bad_alias():
    cells = [{"start_row_offset_idx": "bad", "start_row": 1, "start_col": 0, "text": "A"}]
    table = table_from_span_cells(cells, 2, 1, name="t")
    assert table["rows"] == [["A"]]

