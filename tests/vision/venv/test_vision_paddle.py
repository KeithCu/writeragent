# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PaddleOCR 3.x construction, predict calls, and Result parsing."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
from pytest import MonkeyPatch

from plugin.vision.venv import vision_paddle as paddle_mod
from plugin.vision.venv.vision_paddle import html_from_paddle_regions, html_from_paddle_structure
from plugin.vision.vision_common import MAX_TABLE_ROWS

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


@patch("plugin.vision.venv.vision_paddle.decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_paddle_ocr")
def test_extract_text_v3_ocr_result(mock_get_engine, mock_decode):
    engine = MagicMock(spec=["predict"])
    engine.predict.return_value = [_Result(_v3_ocr_payload())]
    mock_get_engine.return_value = engine
    mock_decode.return_value = MagicMock()

    result = paddle_mod.extract_text(b"png-bytes", {"lang": "en"})

    engine.predict.assert_called_once_with(mock_decode.return_value)
    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"
    assert result["warnings"] == []
    assert "Hello" in result["html"]
    assert "World" in result["html"]
    assert result["metrics"]["line_count"] == 2
    assert result["metrics"]["mean_confidence"] == (0.98 + 0.91) / 2
    assert len(result["regions"]) == 2


@patch("plugin.vision.venv.vision_paddle.decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_paddle_ocr")
def test_extract_text_v3_predict_generator(mock_get_engine, mock_decode):
    mock_get_engine.return_value = _PredictEngine([_Result(_v3_ocr_payload())])
    mock_decode.return_value = MagicMock()

    result = paddle_mod.extract_text(b"png-bytes", {})

    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"


class _V3PaddleOCR:
    """3.x constructor rejects show_log; ocr() is the deprecated predict shim."""

    instances: list[_V3PaddleOCR] = []

    def __init__(self, **kwargs: object) -> None:
        if "show_log" in kwargs:
            raise ValueError("Unknown argument: show_log")
        self.kwargs = kwargs
        self.predict_images: list[object] = []
        self.ocr_called = False
        self.instances.append(self)

    def predict(self, image: object, *, use_textline_orientation: object = None) -> list[_Result]:
        del use_textline_orientation
        self.predict_images.append(image)
        return [_Result(_v3_ocr_payload())]

    def ocr(self, img: object, **kwargs: object) -> list[_Result]:
        # 3.x ocr forwards **kwargs into keyword-only predict, which has no cls.
        self.ocr_called = True
        if kwargs:
            unexpected = next(iter(kwargs))
            raise TypeError(f"predict() got an unexpected keyword argument {unexpected!r}")
        return self.predict(img)


class _V2PaddleOCR:
    """2.x engine: ocr(..., cls=True) and no predict."""

    instances: list[_V2PaddleOCR] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.ocr_calls: list[tuple[object, bool]] = []
        self.instances.append(self)

    def ocr(self, img: object, det: bool = True, rec: bool = True, cls: bool = True):
        del det, rec
        self.ocr_calls.append((img, cls))
        return [_v2_ocr_page()]


class _V3PPStructure:
    """PPStructureV3 rejects unknown kwargs such as show_log."""

    instances: list[_V3PPStructure] = []

    def __init__(self, **kwargs: object) -> None:
        unknown = set(kwargs) - {
            "use_doc_orientation_classify",
            "use_doc_unwarping",
            "use_table_recognition",
            "lang",
        }
        if unknown:
            name = sorted(unknown)[0]
            raise ValueError(f"Unknown argument: {name}")
        self.kwargs = kwargs
        self.predict_images: list[object] = []
        self.instances.append(self)

    def predict(self, image: object) -> list[_LiveStructureResult]:
        self.predict_images.append(image)
        return [_LiveStructureResult(_v3_structure_payload())]


def _install_fake_paddleocr(monkeypatch: MonkeyPatch, fake_mod: SimpleNamespace, *, structure: bool) -> None:
    def _import_module(name: str) -> SimpleNamespace:
        if name != "paddleocr":
            raise ImportError(name)
        return fake_mod

    if structure:
        monkeypatch.setattr(paddle_mod, "_pp_structure_engine", None)
    else:
        monkeypatch.setattr(paddle_mod, "_paddle_ocr_engine", None)
        monkeypatch.setattr(paddle_mod, "_paddle_ocr_lang", None)
    monkeypatch.setattr(paddle_mod.importlib, "import_module", _import_module)
    monkeypatch.setattr(paddle_mod, "decode_image_bytes", lambda image_bytes, scale=1.0: "decoded")


def test_run_paddle_ocr_v3_uses_predict_not_cls() -> None:
    engine = _V3PaddleOCR()
    assert paddle_mod._run_paddle_ocr(engine, "img")[0].json["res"]["rec_texts"][0] == "Hello"
    assert engine.predict_images == ["img"]
    assert engine.ocr_called is False


def test_run_paddle_ocr_v2_passes_cls() -> None:
    engine = _V2PaddleOCR()
    page = paddle_mod._run_paddle_ocr(engine, "img")
    assert page == [_v2_ocr_page()]
    assert engine.ocr_calls == [("img", True)]


def test_extract_text_v3_constructs_without_show_log_and_calls_predict(monkeypatch: MonkeyPatch) -> None:
    _V3PaddleOCR.instances.clear()
    _install_fake_paddleocr(monkeypatch, SimpleNamespace(PaddleOCR=_V3PaddleOCR), structure=False)

    result = paddle_mod.extract_text(b"png-bytes", {"lang": "en"})

    engine = _V3PaddleOCR.instances[-1]
    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"
    assert engine.kwargs == {"use_angle_cls": True, "lang": "en"}
    assert engine.predict_images == ["decoded"]
    assert engine.ocr_called is False


def test_extract_text_v2_constructs_and_calls_ocr_with_cls(monkeypatch: MonkeyPatch) -> None:
    _V2PaddleOCR.instances.clear()
    _install_fake_paddleocr(monkeypatch, SimpleNamespace(PaddleOCR=_V2PaddleOCR), structure=False)

    result = paddle_mod.extract_text(b"png-bytes", {"lang": "en"})

    engine = _V2PaddleOCR.instances[-1]
    assert result["status"] == "ok"
    assert result["full_text"] == "Hello\nWorld"
    assert "show_log" not in engine.kwargs
    assert engine.kwargs["use_angle_cls"] is True
    assert engine.kwargs["lang"] == "en"
    assert engine.ocr_calls == [("decoded", True)]


def test_extract_structure_v3_constructs_without_show_log(monkeypatch: MonkeyPatch) -> None:
    _V3PPStructure.instances.clear()
    _install_fake_paddleocr(monkeypatch, SimpleNamespace(PPStructureV3=_V3PPStructure), structure=True)

    result = paddle_mod.extract_structure(b"png-bytes", {})

    engine = _V3PPStructure.instances[-1]
    assert result["status"] == "ok"
    assert "Invoice" in result["full_text"]
    assert result["metrics"]["table_count"] == 1
    assert engine.kwargs == {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_table_recognition": True,
        "lang": "en",
    }
    assert engine.predict_images == ["decoded"]


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


@patch("plugin.vision.venv.vision_paddle.decode_image_bytes")
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


def test_parse_v3_structure_page_preserves_table_html_positions_when_first_empty():
    # What was wrong: filtering empty strings from htmls shifted indices, causing subsequent tables to be matched to the wrong HTML.
    # Why this change: verify table 2 gets html 2 even when table 1 has empty HTML.
    payload = {
        "table_res_list": [
            {"pred_html": ""},  # empty HTML for table 1
            {"pred_html": "<table><tr><th>T2</th></tr><tr><td>Val2</td></tr></table>"},
        ],
        "parsing_res_list": [
            {"block_label": "table", "block_content": "<table><tr><th>T1</th></tr><tr><td>Val1</td></tr></table>", "block_bbox": [0, 0, 10, 10]},
            {"block_label": "table", "block_content": "", "block_bbox": [0, 10, 10, 20]},
        ],
    }
    blocks, tables, text_parts, _ = paddle_mod._parse_v3_structure_page(payload, table_index=0)
    assert len(tables) == 2
    assert tables[0]["columns"] == ["T1"]
    assert tables[0]["rows"] == [["Val1"]]
    assert tables[1]["columns"] == ["T2"]
    assert tables[1]["rows"] == [["Val2"]]


def test_get_paddle_ocr_attribute_error_guard(monkeypatch: MonkeyPatch):
    # What was wrong: missing AttributeError guard if paddleocr module lacked PaddleOCR class.
    # Why this change: guard (ImportError, AttributeError) and report PADDLEOCR_UNAVAILABLE.
    empty_mod = SimpleNamespace()
    monkeypatch.setattr(paddle_mod, "_paddle_ocr_engine", None)
    monkeypatch.setattr(paddle_mod, "_paddle_ocr_lang", None)
    monkeypatch.setattr(paddle_mod.importlib, "import_module", lambda name: empty_mod)

    result = paddle_mod.extract_text(b"png-bytes", {})
    assert result["status"] == "error"
    assert result["code"] == "PADDLEOCR_UNAVAILABLE"


def test_get_paddle_ocr_constructor_error(monkeypatch: MonkeyPatch):
    # What was wrong: constructor errors in _get_paddle_ocr escaped as unhandled exceptions.
    # Why this change: catch constructor exceptions and return error_result with VISION_ERROR.
    def _exploding_ocr(**kwargs):
        raise RuntimeError("PaddleOCR constructor failed")

    fake_mod = SimpleNamespace(PaddleOCR=_exploding_ocr)
    monkeypatch.setattr(paddle_mod, "_paddle_ocr_engine", None)
    monkeypatch.setattr(paddle_mod, "_paddle_ocr_lang", None)
    monkeypatch.setattr(paddle_mod.importlib, "import_module", lambda name: fake_mod)

    result = paddle_mod.extract_text(b"png-bytes", {})
    assert result["status"] == "error"
    assert result["code"] == "VISION_ERROR"
    assert "PaddleOCR constructor failed" in result["message"]


def test_html_from_paddle_regions_escapes_and_wraps():
    with patch(
        "plugin.vision.venv.vision_paddle.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_regions([{"text": "Line & one"}, {"text": "Line two"}])
    assert "<p>Line &amp; one</p>" in html
    assert "<p>Line two</p>" in html
    assert "Arial" in html


def test_html_from_paddle_structure_table_and_heading():
    with patch(
        "plugin.vision.venv.vision_paddle.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_structure(
            [{"type": "section_header", "text": "Title", "box": [0, 0, 0, 0]}],
            [{"columns": ["A", "B"], "rows": [["1", "2"]]}],
        )
    assert "<h2>Title</h2>" in html
    assert "<table" in html
    assert "<thead>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html
    assert 'border="1"' in html


def test_html_from_paddle_structure_skips_escaped_table_paragraph():
    """Raw PP-Structure ``<table>`` block text must not sit beside the real table."""
    raw_table = "<table><tr><td>Widget</td><td>2</td></tr></table>"
    with patch(
        "plugin.vision.venv.vision_paddle.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_structure(
            [
                {"type": "text", "text": "Invoice"},
                {"type": "table", "text": raw_table, "box": [0, 0, 10, 10]},
            ],
            [{"columns": ["Item", "Qty"], "rows": [["Widget", "2"]]}],
        )
    assert "Invoice" in html
    assert html.lower().count("<table") == 1
    assert "&lt;table" not in html.lower()
    assert "<td>Widget</td>" in html


def test_html_from_paddle_structure_keeps_plain_table_text():
    with patch(
        "plugin.vision.venv.vision_paddle.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_structure(
            [{"type": "table", "text": "See totals"}],
            [{"columns": ["A"], "rows": [["1"]]}],
        )
    assert "See totals" in html
    assert html.lower().count("<table") == 1
    assert "&lt;table" not in html.lower()


def test_decode_image_bytes_composites_transparent_image_onto_white():
    # What was wrong: convert("RGB") on transparent images converted transparent background to black.
    # Why this change: composite alpha channel onto white background before converting to RGB.
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))  # fully transparent
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    arr = paddle_mod.decode_image_bytes(buf.getvalue())
    assert (arr == 255).all()


def test_normalize_structure_pages_keeps_all_region_pages():
    page1 = [{"type": "text", "bbox": [10, 10, 100, 30], "res": [{"text": "One"}]}]
    page2 = [{"type": "text", "bbox": [10, 40, 100, 60], "res": [{"text": "Two"}]}]
    blocks, tables, text_parts = paddle_mod._parse_structure_output(
        paddle_mod._normalize_structure_pages([page1, page2])
    )
    assert text_parts == ["One", "Two"]


def test_parse_structure_decodes_v3_payload_once():
    with patch(
        "plugin.vision.venv.vision_paddle._paddle_payload",
        wraps=paddle_mod._paddle_payload,
    ) as mock_payload:
        paddle_mod._parse_structure_output([_Result(_v3_structure_payload())])
        assert mock_payload.call_count == 1


def test_html_table_parser_clamps_huge_spans():
    html = '<table><tr><td rowspan="1000" colspan="1000">x</td></tr></table>'
    table = paddle_mod._table_from_structure_res({"html": html}, name="t")
    assert table is not None
    assert len(table["columns"]) <= MAX_TABLE_ROWS
    assert table["total_rows"] <= MAX_TABLE_ROWS


def test_html_table_parser_nested_table_keeps_outer_row():
    html = "<table><tr><td>outer<table><tr><td>inner</td></tr></table>tail</td><td>b</td></tr></table>"
    table = paddle_mod._table_from_structure_res({"html": html}, name="t")
    assert table is not None
    assert table["columns"] == ["outer tail", "b"]
    assert table["rows"] == []


def test_html_table_parser_br_and_nbsp():
    html = "<table><tr><td>Line&nbsp;one<br>Line two</td><td>x</td></tr></table>"
    table = paddle_mod._table_from_structure_res({"html": html}, name="t")
    assert table is not None
    assert table["columns"] == ["Line one Line two", "x"]


def test_decode_image_bytes_applies_images_scale():
    img = Image.new("RGB", (10, 10), (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    assert paddle_mod.decode_image_bytes(data).shape == (10, 10, 3)
    assert paddle_mod.decode_image_bytes(data, scale=2.0).shape == (20, 20, 3)
    assert paddle_mod.decode_image_bytes(data, scale=0.5).shape == (5, 5, 3)


@patch("plugin.vision.venv.vision_paddle.decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_paddle_ocr")
def test_extract_text_passes_images_scale(mock_get_engine, mock_decode):
    mock_get_engine.return_value = _PredictEngine([_Result(_v3_ocr_payload())])
    mock_decode.return_value = MagicMock()

    result = paddle_mod.extract_text(b"png-bytes", {"images_scale": 2.0})

    mock_decode.assert_called_once_with(b"png-bytes", scale=2.0)
    assert result["status"] == "ok"


@patch("plugin.vision.venv.vision_paddle.decode_image_bytes")
@patch("plugin.vision.venv.vision_paddle._get_pp_structure")
def test_extract_structure_passes_images_scale(mock_get_engine, mock_decode):
    mock_get_engine.return_value = _PredictEngine([_Result(_v3_structure_payload())])
    mock_decode.return_value = MagicMock()

    result = paddle_mod.extract_structure(b"png-bytes", {"images_scale": 2.0})

    mock_decode.assert_called_once_with(b"png-bytes", scale=2.0)
    assert result["status"] == "ok"


def test_parse_ocr_v2_bad_confidence_does_not_abort():
    regions, texts = paddle_mod._parse_ocr_v2_lines([
        [_quad(1, 1, 4, 2), ("Hello", "N/A")],
        [_quad(1, 5, 4, 2), ("World", None)],
    ])
    assert texts == ["Hello", "World"]
    assert [region["confidence"] for region in regions] == [0.0, 0.0]
