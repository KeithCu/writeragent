# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PaddleOCR 3.x Result parsing for extract_text and extract_structure."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.vision.venv import vision_paddle as paddle_mod

_TABLE_HTML = "<html><body><table><tr><th>Item</th><th>Qty</th></tr><tr><td>Widget</td><td>2</td></tr></table></body></html>"


class _Result:
    """PaddleX Result stand-in. ``.json`` is ``{"res": payload}``."""

    def __init__(self, payload: dict):
        self._payload = payload

    @property
    def json(self):
        return {"res": self._payload}


class _LayoutBlock:
    def __init__(self, label: str, content: str, bbox: list[int]):
        self.label = label
        self.content = content
        self.bbox = bbox


class _LiveStructureResult(dict):
    """Live PPStructureV3 page: dict body is LayoutBlocks, schema is on ``.json``."""

    def __init__(self, payload: dict):
        super().__init__(parsing_res_list=[_LayoutBlock("text", "IGNORED", [1, 1, 2, 2])], table_res_list=[])
        self._payload = payload

    @property
    def json(self):
        return {"res": self._payload}


class _PredictEngine:
    def __init__(self, pages: list):
        self._pages = pages

    def predict(self, image):
        del image
        yield from self._pages


def _quad(x: int, y: int, w: int, h: int) -> list[list[int]]:
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def _v2_ocr_page() -> list:
    return [
        [[[10, 10], [50, 10], [50, 30], [10, 30]], ("Hello", 0.98)],
        [[[10, 40], [60, 40], [60, 60], [10, 60]], ("World", 0.91)],
    ]


def _v3_ocr_payload() -> dict:
    return {
        "rec_texts": ["Hello", "", "World"],
        "rec_scores": [0.98, 0.10, 0.91],
        "rec_polys": [_quad(10, 10, 40, 20), _quad(1, 1, 2, 2), _quad(10, 40, 50, 20)],
    }


def _v3_structure_payload() -> dict:
    return {
        "parsing_res_list": [
            {"block_label": "doc_title", "block_content": "Invoice", "block_bbox": [10, 10, 100, 30], "block_id": 0, "block_order": 1},
            {"block_label": "table", "block_content": "", "block_bbox": [10, 40, 200, 120], "block_id": 1, "block_order": 2},
            {"block_label": "image", "block_content": "", "block_bbox": [0, 0, 10, 10], "block_id": 2, "block_order": None},
        ],
        "table_res_list": [{"pred_html": _TABLE_HTML}],
    }


def test_parse_ocr_lines_v2_page_and_v3_result():
    regions, texts = paddle_mod._parse_ocr_lines([_v2_ocr_page()])
    assert texts == ["Hello", "World"]
    assert regions[0]["box"] == [10, 10, 40, 20]
    assert regions[0]["confidence"] == 0.98

    regions, texts = paddle_mod._parse_ocr_lines({"res": _v3_ocr_payload()})
    assert texts == ["Hello", "World"]
    assert [region["box"] for region in regions] == [[10, 10, 40, 20], [10, 40, 50, 20]]
    assert regions[1]["confidence"] == 0.91


@patch("plugin.vision.venv.vision_paddle._decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_paddle_ocr")
def test_extract_text_v3_ocr_result(mock_get_engine, mock_decode):
    engine = MagicMock()
    engine.ocr.return_value = [_Result(_v3_ocr_payload())]
    mock_get_engine.return_value = engine
    mock_decode.return_value = MagicMock()

    result = paddle_mod.extract_text(b"png-bytes", {"lang": "en"})

    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"
    assert result["warnings"] == []
    assert "Hello" in result["html"]
    assert "World" in result["html"]
    assert result["metrics"]["line_count"] == 2
    assert result["metrics"]["mean_confidence"] == (0.98 + 0.91) / 2
    assert len(result["regions"]) == 2


@patch("plugin.vision.venv.vision_paddle._decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_paddle_ocr")
def test_extract_text_v3_predict_generator(mock_get_engine, mock_decode):
    mock_get_engine.return_value = _PredictEngine([_Result(_v3_ocr_payload())])
    mock_decode.return_value = MagicMock()

    result = paddle_mod.extract_text(b"png-bytes", {})

    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"


def test_parse_structure_v2_regions_and_v3_page():
    v2_page = [
        {"type": "text", "bbox": [10, 10, 100, 30], "res": [{"text": "Invoice"}]},
        {
            "type": "table",
            "bbox": [10, 40, 200, 120],
            "res": {"html": "<table><tr><th>Item</th><th>Qty</th></tr><tr><td>Widget</td><td>2</td></tr></table>"},
        },
    ]
    blocks, tables, text_parts = paddle_mod._parse_structure_output(paddle_mod._normalize_structure_pages([v2_page]))
    assert "Invoice" in text_parts
    assert tables[0]["columns"] == ["Item", "Qty"]
    assert tables[0]["rows"] == [["Widget", "2"]]
    assert "<table" not in blocks[1]["text"].lower()

    # 2.x callers that stuffed regions under parsing_res_list must still unwrap.
    wrapped = {"parsing_res_list": [v2_page[0]]}
    blocks, tables, text_parts = paddle_mod._parse_structure_output(paddle_mod._normalize_structure_pages(wrapped))
    assert text_parts == ["Invoice"]
    assert tables == []

    blocks, tables, text_parts = paddle_mod._parse_structure_output(paddle_mod._normalize_structure_pages({"res": _v3_structure_payload()}))
    assert text_parts[0] == "Invoice"
    assert tables[0]["columns"] == ["Item", "Qty"]
    assert tables[0]["rows"] == [["Widget", "2"]]
    assert [block["type"] for block in blocks] == ["doc_title", "table"]
    assert blocks[1]["text"] == ""
    assert blocks[0]["box"] == [10, 10, 90, 20]


def test_parse_structure_prefers_result_json_over_layout_blocks():
    page = _LiveStructureResult(_v3_structure_payload())
    blocks, tables, text_parts = paddle_mod._parse_structure_output(paddle_mod._normalize_structure_pages(page))
    assert "IGNORED" not in text_parts
    assert text_parts[0] == "Invoice"
    assert tables[0]["rows"] == [["Widget", "2"]]
    assert blocks[1]["type"] == "table"


@patch("plugin.vision.venv.vision_paddle._decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_pp_structure")
def test_extract_structure_v3_predict_result(mock_get_engine, mock_decode):
    mock_get_engine.return_value = _PredictEngine([_LiveStructureResult(_v3_structure_payload())])
    mock_decode.return_value = MagicMock()

    result = paddle_mod.extract_structure(b"png-bytes", {})

    assert result["status"] == "ok"
    assert "Invoice" in result["full_text"]
    assert "Widget" in result["full_text"]
    assert result["metrics"]["table_count"] == 1
    assert result["metrics"]["block_count"] == 2
    assert result["tables"][0]["columns"] == ["Item", "Qty"]
    assert result["html"].lower().count("<table") == 1
    assert "&lt;table" not in result["html"].lower()
    assert result["warnings"] == []
