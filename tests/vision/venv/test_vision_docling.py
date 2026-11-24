# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Docling vision backend adapter."""

from __future__ import annotations

import dataclasses
import importlib
import importlib.metadata
import logging
import shutil
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from plugin.vision import vision_common as vision_common_mod
from plugin.vision.venv import vision_docling as docling_mod
from plugin.vision.venv.vision_docling import export_docling_to_html, extract_structure, extract_text
from plugin.vision.vision_common import DOCLING_MIN_VERSION, DOCLING_REQUIREMENT


def _require_supported_docling() -> None:
    """Skip unless Docling is installed and meets DOCLING_MIN_VERSION."""
    pytest.importorskip("docling.document_converter")
    try:
        docling_mod._check_docling_version()
    except docling_mod.DoclingApiMismatchError as exc:
        pytest.skip(str(exc))


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


def _rows_table(rows: list[list[str]], prov: list | None = None) -> dict:
    cells = [{"text": t, "start_row_offset_idx": r, "start_col_offset_idx": c, "row_span": 1, "col_span": 1} for r, row in enumerate(rows) for c, t in enumerate(row)]
    return {"prov": [] if prov is None else prov, "data": {"num_rows": len(rows), "num_cols": len(rows[0]), "table_cells": cells}}


@patch(
    "plugin.vision.venv.vision_docling.export_docling_to_html",
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
    "plugin.vision.venv.vision_docling.export_docling_to_html",
    return_value="<h2>Title</h2><table></table>",
)
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_structure_maps_tables(mock_convert, _mock_html):
    mock_convert.return_value = _mock_document(
        texts=[{"text": "Title", "label": "section_header", "prov": []}],
        tables=[_rows_table([["A", "B"], ["1", "2"]])],
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
        "plugin.vision.venv.vision_docling.export_docling_to_html",
        return_value="<table></table>",
    ), patch("plugin.vision.venv.vision_docling._convert_image_bytes", return_value=mock_convert_doc):
        result = extract_structure(b"png", {})

    table = result["tables"][0]
    assert table["columns"] == ["ASSETS:", "", ""]
    assert table["rows"] == [["Cash", "10", "11"]]
    assert table["spans"] == [{"row": 0, "col": 0, "rowspan": 1, "colspan": 3}]
    assert table["columns"].count("ASSETS:") == 1


def test_want_full_page_ocr_skips_pdfs():
    P = docling_mod.DoclingPipelineParams.from_params
    assert P({}, input_format="image").full_page_ocr is True
    assert P({}, input_format="pdf").full_page_ocr is False
    assert P({"ocr_mode": "full_page"}, input_format="pdf").full_page_ocr is True
    assert P({"force_full_page_ocr": True}, input_format="pdf").full_page_ocr is False


def test_extract_text_docling_missing():
    with patch("plugin.vision.venv.vision_docling._convert_image_bytes", side_effect=ImportError("docling is not installed")):
        result = extract_text(b"png", {})

    assert result["status"] == "error"
    assert result["code"] == "DOCLING_UNAVAILABLE"
    assert docling_mod._DOCLING_INSTALL_CMD in result["message"]


def test_extract_text_unknown_ocr_backend():
    with patch(
        "plugin.vision.venv.vision_docling._convert_image_bytes",
        side_effect=ValueError("Unknown ocr_backend 'nope'"),
    ):
        result = extract_text(b"png", {"ocr_backend": "nope"})

    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAM"

    with patch(
        "plugin.vision.venv.vision_docling._convert_image_bytes",
        side_effect=docling_mod.OcrBackendError("rapidocr is not installed"),
    ):
        result2 = extract_text(b"png", {"ocr_backend": "rapidocr"})

    assert result2["status"] == "error"
    assert result2["code"] == "OCR_BACKEND_UNAVAILABLE"


def test_apply_pipeline_params_maps_flat_keys():
    pipeline = MagicMock()
    table_opts = MagicMock()
    layout_opts = MagicMock()
    pipeline.table_structure_options = table_opts
    pipeline.layout_options = layout_opts

    with patch("plugin.vision.venv.vision_docling._resolve_layout_model_spec", return_value="heron-spec"):
        fast_mode = MagicMock()
        mock_pipeline_mod = MagicMock()
        mock_pipeline_mod.TableFormerMode.FAST = fast_mode
        with patch("importlib.import_module", return_value=mock_pipeline_mod):
            docling_mod._apply_pipeline_params(
                pipeline,
                docling_mod.DoclingPipelineParams.from_params(
                    {
                        "images_scale": 2.0,
                        "document_timeout": 120,
                        "device": "cpu",
                        "num_threads": 8,
                        "table_mode": "fast",
                        "do_cell_matching": False,
                        "create_orphan_clusters": False,
                        "layout_model": "heron",
                    }
                ),
            )

    assert pipeline.images_scale == 2.0
    assert pipeline.document_timeout == 120
    mock_pipeline_mod.AcceleratorOptions.assert_called_once_with(device="cpu", num_threads=8)
    assert pipeline.accelerator_options is mock_pipeline_mod.AcceleratorOptions.return_value
    assert table_opts.mode == fast_mode
    assert table_opts.do_cell_matching is False
    assert layout_opts.create_orphan_clusters is False
    assert layout_opts.model_spec == "heron-spec"


def test_build_pipeline_options_real_docling_layout_has_get_engine_config():
    """Requires a supported installed Docling; skips otherwise. No importlib mock.

    Catches issue 587 on a real install: OD layout model_spec must expose
    get_engine_config after WriterAgent builds pipeline options.
    """
    _require_supported_docling()
    opts = docling_mod._build_pipeline_options(
        docling_mod.DoclingPipelineParams.from_params({"ocr_backend": "rapidocr", "layout_model": "heron"}),
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
        "ocr_models_missing",
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
    _require_supported_docling()
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

    _require_supported_docling()
    pytest.importorskip("PIL.Image")
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
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


def test_apply_pipeline_params_does_not_swallow_layout_resolution_error():
    pipeline = MagicMock()
    pipeline.layout_options = MagicMock()
    pipeline.table_structure_options = MagicMock()
    pipeline.accelerator_options = MagicMock()
    with patch.object(docling_mod, "_resolve_layout_model_spec", side_effect=RuntimeError("spec unavailable")):
        with pytest.raises(RuntimeError, match="spec unavailable"):
            docling_mod._apply_pipeline_params(
                pipeline, docling_mod.DoclingPipelineParams.from_params({})
            )


def test_text_score_zero_is_its_own_cache_key():
    P = docling_mod.DoclingPipelineParams.from_params
    missing, default, blank = P({}), P({"text_score": 0.5}), P({"text_score": ""})
    zero, zero_int, zero_str = P({"text_score": 0.0}), P({"text_score": 0}), P({"text_score": "0.0"})
    assert missing == default == blank
    assert zero == zero_int == zero_str
    assert zero != default
    assert zero.text_score == 0.0
    assert missing.text_score == 0.5


def test_converter_cache_does_not_reuse_default_for_text_score_zero():
    """A warm 0.5 converter must not be returned when the threshold is 0.0."""
    built: list[float] = []

    def fake_build(params, *, input_format="image"):
        score = params.text_score
        built.append(score)
        pipeline = MagicMock()
        pipeline.captured_text_score = score
        return pipeline

    base_models = MagicMock()
    base_models.InputFormat.IMAGE = "IMAGE"
    created: list[MagicMock] = []

    def fake_converter(**kwargs):
        conv = MagicMock()
        conv.kwargs = kwargs
        created.append(conv)
        return conv

    converter_mod = MagicMock()
    converter_mod.DocumentConverter.side_effect = fake_converter
    converter_mod.ImageFormatOption.side_effect = lambda **kw: kw

    def fake_import(name):
        if name == "docling.datamodel.base_models":
            return base_models
        if name == "docling.document_converter":
            return converter_mod
        raise ImportError(name)

    with (
        patch.object(docling_mod, "_check_docling_version"),
        patch.object(docling_mod, "_build_pipeline_options", side_effect=fake_build),
        patch.object(docling_mod.importlib, "import_module", side_effect=fake_import),
    ):
        first = docling_mod._get_docling_converter(docling_mod.DoclingPipelineParams.from_params({"text_score": 0.5}))
        again = docling_mod._get_docling_converter(docling_mod.DoclingPipelineParams.from_params({"text_score": 0.5}))
        second = docling_mod._get_docling_converter(docling_mod.DoclingPipelineParams.from_params({"text_score": 0.0}))

    assert first is again
    assert first is not second
    assert built == [0.5, 0.0]
    assert created[1].kwargs["format_options"]["IMAGE"]["pipeline_options"].captured_text_score == 0.0
    assert len(docling_mod._converter_cache) == 1
    assert list(docling_mod._converter_cache) == [docling_mod.DoclingPipelineParams.from_params({"text_score": 0.0})]



def test_rowspan_past_max_table_rows_is_clipped(monkeypatch):
    from plugin.vision import vision_common

    monkeypatch.setattr(vision_common, "MAX_TABLE_ROWS", 2)
    cells = [
        {"text": "H", "start_row_offset_idx": 0, "start_col_offset_idx": 0, "row_span": 10, "col_span": 1},
        {"text": "A", "start_row_offset_idx": 1, "start_col_offset_idx": 1, "row_span": 1, "col_span": 1},
        {"text": "B", "start_row_offset_idx": 2, "start_col_offset_idx": 1, "row_span": 1, "col_span": 1},
        {"text": "C", "start_row_offset_idx": 3, "start_col_offset_idx": 1, "row_span": 4, "col_span": 1},
    ]
    table = vision_common.table_from_span_cells(cells, num_rows=6, num_cols=2, name="t")
    assert table is not None
    assert table["truncated"] is True
    assert len(table["rows"]) == 2
    spans = table["spans"]
    assert spans == [{"row": 0, "col": 0, "rowspan": 3, "colspan": 1}]


def test_cell_text_empty_returns_empty_string():
    # Empty cell.text must not fall through to the TableCell object; _cell_text returns "" not repr.
    from plugin.vision import vision_common

    class FakeTableCell:
        text = ""

        def __str__(self):
            return "TableCell(text='', start_row_offset_idx=0)"

    cell = FakeTableCell()
    assert vision_common._cell_text(cell) == ""


@patch("plugin.vision.venv.vision_docling.export_docling_to_html", return_value="<p>Doc</p>")
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_extract_text_includes_table_text_and_omits_mean_confidence(mock_convert, _mock_html):
    # full_text must include table TSV, not paragraphs only; Docling has no scores so mean_confidence stays out of metrics.
    mock_convert.return_value = _mock_document(
        texts=[{"text": "Headline", "prov": []}],
        tables=[
            {
                "prov": [],
                "data": {
                    "num_rows": 2,
                    "num_cols": 2,
                    "table_cells": [
                        {"text": "ColA", "start_row_offset_idx": 0, "start_col_offset_idx": 0, "row_span": 1, "col_span": 1},
                        {"text": "ColB", "start_row_offset_idx": 0, "start_col_offset_idx": 1, "row_span": 1, "col_span": 1},
                        {"text": "ValA", "start_row_offset_idx": 1, "start_col_offset_idx": 0, "row_span": 1, "col_span": 1},
                        {"text": "ValB", "start_row_offset_idx": 1, "start_col_offset_idx": 1, "row_span": 1, "col_span": 1},
                    ],
                },
            }
        ],
    )
    result = extract_text(b"png-bytes", {})
    assert result["status"] == "ok"
    assert "Headline" in result["full_text"]
    assert "ColA\tColB" in result["full_text"]
    assert "ValA\tValB" in result["full_text"]
    assert "mean_confidence" not in result["metrics"]


def test_export_docling_to_html_default():
    doc = MagicMock()
    doc.export_to_html.return_value = "<p><strong>Hi</strong></p>"
    fake = MagicMock()
    fake.ImageRefMode.PLACEHOLDER = "placeholder"
    with patch("plugin.vision.venv.vision_docling.importlib.import_module", return_value=fake), patch(
        "plugin.vision.venv.vision_docling.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        out = export_docling_to_html(doc)
    assert "strong" in out
    doc.export_to_html.assert_called_once_with(
        image_mode=fake.ImageRefMode.PLACEHOLDER,
        formula_to_mathml=True,
        split_page_view=False,
    )


def test_export_docling_to_html_returns_empty_when_no_export_method():
    obj = object()
    out = export_docling_to_html(obj)
    assert out == ""


def test_module_yaml_layout_models_supported_in_od_presets():
    yaml_path = Path(__file__).resolve().parents[3] / "plugin" / "vision" / "module.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    options = data["config"]["layout_model"]["options"]
    layout_values = [opt["value"] for opt in options]
    for val in layout_values:
        assert val in docling_mod._LAYOUT_KEYS


def test_from_params_parses_string_booleans():
    p = docling_mod.DoclingPipelineParams.from_params(
        {
            "do_cell_matching": "false",
            "create_orphan_clusters": "0",
            "do_formula_enrichment": "off",
            "force_full_page_ocr": "no",
            "do_code_enrichment": "true",
        }
    )
    assert (
        p.do_cell_matching,
        p.create_orphan_clusters,
        p.do_formula_enrichment,
        p.full_page_ocr,
        p.do_code_enrichment,
    ) == (False, False, False, False, True)

    default_p = docling_mod.DoclingPipelineParams.from_params({})
    assert default_p.do_cell_matching is True
    assert default_p.full_page_ocr is True
    assert default_p.do_formula_enrichment is False
    assert default_p.do_code_enrichment is False


@patch("plugin.vision.venv.vision_docling.export_docling_to_html", return_value="<p>A</p>")
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_provenance_bbox_page_height_flips_bottom_left_coords(mock_convert, _mock_html):
    doc = _mock_document(
        texts=[{"text": "A", "prov": [{"page_no": 1, "bbox": {"l": 0, "t": 790, "r": 40, "b": 750, "coord_origin": "BOTTOMLEFT"}}]}],
    )
    doc.pages = {1: SimpleNamespace(size=SimpleNamespace(height=800))}
    mock_convert.return_value = doc

    result = extract_text(b"png", {"ocr_backend": "rapidocr_paddle"})
    assert result["regions"][0]["box"] == [0, 10, 40, 40]


@patch("plugin.vision.venv.vision_docling.export_docling_to_html", return_value="<table></table>")
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_provenance_table_bbox_page_height_flips_bottom_left_coords(mock_convert, _mock_html):
    doc = _mock_document(
        texts=[],
        tables=[
            _rows_table(
                [["A"], ["1"]],
                prov=[{"page_no": 1, "bbox": {"l": 0, "t": 790, "r": 40, "b": 750, "coord_origin": "BOTTOMLEFT"}}],
            )
        ],
    )
    doc.pages = {1: SimpleNamespace(size=SimpleNamespace(height=800))}
    mock_convert.return_value = doc

    result = extract_structure(b"png", {"ocr_backend": "rapidocr_paddle"})
    table_blocks = [b for b in result["blocks"] if b.get("type") == "table"]
    assert len(table_blocks) == 1
    assert table_blocks[0]["box"] == [0, 10, 40, 40]


@patch("plugin.vision.venv.vision_docling.export_docling_to_html", return_value="<p>A</p>")
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_provenance_bbox_bottom_left_without_page_height_degrades_to_zero(mock_convert, _mock_html):
    doc = _mock_document(
        texts=[{"text": "A", "prov": [{"page_no": 1, "bbox": {"l": 0, "t": 790, "r": 40, "b": 750, "coord_origin": "BOTTOMLEFT"}}]}],
    )
    doc.pages = {}
    mock_convert.return_value = doc

    result = extract_text(b"png", {"ocr_backend": "rapidocr_paddle"})
    assert result["regions"][0]["box"] == [0, 0, 0, 0]


@patch("plugin.vision.venv.vision_docling.export_docling_to_html", return_value="")
@patch("plugin.vision.venv.vision_docling._convert_image_bytes")
def test_unparsed_table_does_not_add_empty_block(mock_convert, _mock_html):
    mock_convert.return_value = _mock_document(texts=[], tables=[{"prov": [], "data": {}}], markdown="")
    result = extract_structure(b"png", {"ocr_backend": "rapidocr_paddle"})
    assert result["metrics"]["block_count"] == 0
    assert not any(b.get("type") == "table" for b in result["blocks"])


@pytest.mark.parametrize("result", [SimpleNamespace(document=None), SimpleNamespace()])
def test_extract_text_docling_returned_no_document(result):
    """Regression (#2688): a conversion with no document must be a clear error."""
    import importlib as _importlib

    real_import = _importlib.import_module
    converter = MagicMock()
    converter.convert.return_value = result

    def fake_import(name, *args, **kwargs):
        if name == "docling.datamodel.base_models":
            return MagicMock()
        return real_import(name, *args, **kwargs)

    with (
        patch.object(docling_mod.importlib, "import_module", side_effect=fake_import),
        patch.object(docling_mod, "_get_docling_converter", return_value=converter),
        patch.object(docling_mod, "export_docling_to_html") as mock_html,
    ):
        with pytest.raises(RuntimeError, match="returned no document"):
            docling_mod._convert_image_bytes(b"png", {})
        out = extract_text(b"png", {})

    assert out["status"] == "error"
    assert "returned no document" in out["message"]
    mock_html.assert_not_called()


def test_load_models_then_extract_reuses_one_converter():
    docling_mod._converter_cache.clear()
    base_models = MagicMock()
    base_models.InputFormat.IMAGE = "IMAGE"
    created = []

    def fake_converter(**kwargs):
        conv = MagicMock()
        conv.convert.return_value = MagicMock(document=MagicMock())
        created.append(conv)
        return conv

    converter_mod = MagicMock()
    converter_mod.DocumentConverter.side_effect = fake_converter
    converter_mod.ImageFormatOption.side_effect = lambda **kw: kw

    def fake_import(name):
        if name == "docling.datamodel.base_models":
            return base_models
        if name == "docling.document_converter":
            return converter_mod
        raise ImportError(name)

    with (
        patch.object(docling_mod, "_build_pipeline_options", return_value=MagicMock()),
        patch.object(docling_mod.importlib, "import_module", side_effect=fake_import),
    ):
        loaded = docling_mod.load_models({"layout_model": "heron"})
        docling_mod._convert_image_bytes(b"\x89PNG\r\n\x1a\n", {"layout_model": "heron"})

    assert loaded == {"status": "ok", "helper": "load_models"}
    assert len(created) == 1
    created[0].initialize_pipeline.assert_called_once_with("IMAGE")
    created[0].convert.assert_called_once()


def test_load_models_maps_import_error():
    docling_mod._converter_cache.clear()
    with patch.object(docling_mod, "_get_docling_converter", side_effect=ImportError("No module named 'docling'")):
        out = docling_mod.load_models({})
    assert out["status"] == "error"
    assert out["code"] == "DOCLING_UNAVAILABLE"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", ("iso:en",)),
        ("en, de,en", ("iso:en", "iso:de")),
        ("en_US", ("iso:en_US",)),
        ("zh-TW", ("iso:zh-TW",)),
        ("japan", ("japan",)),
        ("chinese_cht", ("chinese_cht",)),
        ("script/Cyrillic", ("script/Cyrillic",)),
        ("iso:zh-Hant", ("iso:zh-Hant",)),
        (["en", " de "], ("iso:en", "iso:de")),
        (None, ("iso:en",)),
        ("", ("iso:en",)),
        (",,", ("iso:en",)),
    ],
)
def test_docling_ocr_langs(raw, expected):
    assert docling_mod.docling_ocr_langs(raw) == expected


def test_from_params_ru_gets_iso_prefix():
    assert docling_mod.DoclingPipelineParams.from_params({"lang": "ru"}).lang == ("iso:ru",)


def test_installed_ocr_backend_missing_module_names_install_line():
    with patch.dict(sys.modules, {"easyocr": None}):
        with pytest.raises(docling_mod.OcrBackendError, match="pip install easyocr"):
            docling_mod._installed_ocr_backend("easyocr")


def test_installed_ocr_backend_tesseract_needs_program():
    with patch("shutil.which", return_value=None):
        with pytest.raises(docling_mod.OcrBackendError, match="tesseract.*PATH"):
            docling_mod._installed_ocr_backend("tesseract")


@pytest.mark.parametrize("backend", ["surya", "auto", "rapidocr_onnx"])
def test_installed_ocr_backend_rejects_unknown(backend):
    with pytest.raises(docling_mod.OcrBackendError, match=f"Unknown ocr_backend {backend!r}"):
        docling_mod._installed_ocr_backend(backend)


def test_ocr_language_unsupported_maps_to_code():
    class OcrLanguageNotSupportedError(RuntimeError):
        def __init__(self, msg: str, *, language: str, engine: str):
            super().__init__(msg)
            self.language = language
            self.engine = engine

    err = OcrLanguageNotSupportedError("no model for ru", language="ru", engine="RapidOCR")
    with patch("plugin.vision.venv.vision_docling._convert_image_bytes", side_effect=err):
        res = extract_text(b"png", {"lang": "ru"})

    assert res["status"] == "error"
    assert res["code"] == "OCR_LANGUAGE_UNSUPPORTED"
    assert res["details"] == {"language": "ru", "engine": "RapidOCR"}


def test_settings_ocr_backend_options_are_registry_ids():
    yaml_path = Path(__file__).resolve().parents[3] / "plugin" / "vision" / "module.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    cfg = data["config"]["ocr_backend"]
    assert cfg["default"] == "rapidocr"
    for opt in cfg["options"]:
        assert opt["value"] in vision_common_mod.OCR_BACKENDS


@pytest.mark.parametrize(
    ("backend_id", "expected_kind", "expected_rapid_backend", "threshold_field"),
    [
        ("rapidocr", "rapidocr", "onnxruntime", "text_score"),
        ("rapidocr_openvino", "rapidocr", "openvino", "text_score"),
        ("rapidocr_paddle", "rapidocr", "paddle", "text_score"),
        ("rapidocr_torch", "rapidocr", "torch", "text_score"),
        ("easyocr", "easyocr", None, "confidence_threshold"),
        ("tesseract", "tesseract", None, None),
        ("nemotron", "nemotron-ocr", None, None),
        ("ocrmac", "ocrmac", None, None),
    ],
)
def test_resolve_ocr_options_real_docling_builds_each_backend(
    backend_id, expected_kind, expected_rapid_backend, threshold_field
):
    _require_supported_docling()
    norm = docling_mod.DoclingPipelineParams.from_params(
        {
            "ocr_backend": backend_id,
            "lang": "en",
            "text_score": 0.3,
            "rapidocr_model_size": "medium",
            "rapidocr_detect_orientation": True,
            "ocrmac_recognition": "fast",
        }
    )
    with (
        patch.object(sys, "platform", "darwin"),
        patch.object(docling_mod, "_import_engine_module"),
        patch("shutil.which", return_value="/usr/bin/tesseract"),
    ):
        opts = docling_mod._resolve_ocr_options(norm)

    assert opts.kind == expected_kind
    assert opts.lang == ["iso:en-Latn"]
    if threshold_field is not None:
        assert getattr(opts, threshold_field) == 0.3
    if backend_id == "tesseract":
        assert opts.tesseract_cmd == "/usr/bin/tesseract"
    if backend_id == "ocrmac":
        assert opts.recognition == "fast"
    if expected_rapid_backend is not None:
        assert opts.backend == expected_rapid_backend
        assert opts.model_size == "medium"
        assert opts.detect_orientation is True


def test_resolve_ocr_options_easyocr_gets_text_score_as_confidence_threshold():
    fake_mod = SimpleNamespace(
        OcrMode=SimpleNamespace(FULL_PAGE="full", DEFAULT="default"),
        EasyOcrOptions=dict,
    )
    norm = docling_mod.DoclingPipelineParams.from_params({"ocr_backend": "easyocr", "text_score": 0.3})
    with (
        patch.dict(sys.modules, {"docling.datamodel.pipeline_options": fake_mod}),
        patch.object(docling_mod, "_import_engine_module"),
    ):
        opts = docling_mod._resolve_ocr_options(norm)

    assert opts["confidence_threshold"] == 0.3
    for forbidden in ("text_score", "backend", "model_size"):
        assert forbidden not in opts


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_installed_ocr_backend_ocrmac_needs_macos(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    with (
        patch.object(docling_mod, "_import_engine_module"),
        pytest.raises(docling_mod.OcrBackendError, match=r"does not run on this system.*macOS"),
    ):
        docling_mod._installed_ocr_backend("ocrmac")


def test_installed_ocr_backend_ocrmac_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with patch.object(docling_mod, "_import_engine_module"):
        row, program = docling_mod._installed_ocr_backend("ocrmac")
    assert row == vision_common_mod.OCR_BACKENDS["ocrmac"]
    assert program == ""


def test_resolve_ocr_options_ocrmac_gets_recognition(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fake_mod = SimpleNamespace(
        OcrMode=SimpleNamespace(FULL_PAGE="full", DEFAULT="default"),
        OcrMacOptions=dict,
    )
    norm = docling_mod.DoclingPipelineParams.from_params(
        {"ocr_backend": "ocrmac", "ocrmac_recognition": "fast", "text_score": 0.3}
    )
    with (
        patch.dict(sys.modules, {"docling.datamodel.pipeline_options": fake_mod}),
        patch.object(docling_mod, "_import_engine_module"),
    ):
        opts = docling_mod._resolve_ocr_options(norm)

    assert opts["recognition"] == "fast"
    for forbidden in ("text_score", "confidence_threshold", "backend", "model_size"):
        assert forbidden not in opts


def test_from_params_ocrmac_recognition():
    default = docling_mod.DoclingPipelineParams.from_params({})
    assert default.ocrmac_recognition == "accurate"

    custom = docling_mod.DoclingPipelineParams.from_params({"ocrmac_recognition": " Fast "})
    assert custom.ocrmac_recognition == "fast"
    assert custom != default

    yaml_path = Path(__file__).resolve().parents[3] / "plugin" / "vision" / "module.yaml"
    cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["config"]["ocrmac_recognition"]
    for opt in cfg["options"]:
        parsed = docling_mod.DoclingPipelineParams.from_params({"ocrmac_recognition": opt["value"]})
        assert parsed.ocrmac_recognition == opt["value"]

    res = extract_text(b"png", {"ocrmac_recognition": "bogus"})
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"


@pytest.mark.timeout(300)
def test_extract_text_real_easyocr_reads_text():
    _require_supported_docling()
    pytest.importorskip("easyocr")
    pytest.importorskip("css_inline")

    result = extract_text(
        _tiny_hello_ocr_png(),
        {"layout_model": "heron", "ocr_backend": "easyocr", "lang": "en"},
    )
    if result.get("status") != "ok" and _is_missing_weight_or_network_error(result):
        pytest.skip(f"Docling convert needs weights/network: {result.get('message')}")
    assert result["status"] == "ok"
    assert "hello" in result["full_text"].lower()


@pytest.mark.timeout(300)
def test_extract_text_real_easyocr_incompatible_languages_is_invalid_param():
    _require_supported_docling()
    pytest.importorskip("easyocr")
    pytest.importorskip("css_inline")

    result = extract_text(
        _tiny_hello_ocr_png(),
        {"layout_model": "heron", "ocr_backend": "easyocr", "lang": "ja,ko"},
    )
    if result.get("status") != "ok" and _is_missing_weight_or_network_error(result):
        pytest.skip(f"Docling convert needs weights/network: {result.get('message')}")
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAM"


def test_extract_text_real_docling_rejects_bad_device():
    _require_supported_docling()
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    res = extract_text(_tiny_hello_ocr_png(), {"device": "bogus"})
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"


@pytest.mark.timeout(300)
def test_extract_text_real_docling_non_latin_lang():
    _require_supported_docling()
    pytest.importorskip("PIL.Image")
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("css_inline")

    result = extract_text(
        _tiny_hello_ocr_png(),
        {"layout_model": "heron", "ocr_backend": "rapidocr", "lang": "ru"},
    )
    if result.get("status") != "ok" and _is_missing_weight_or_network_error(result):
        pytest.skip(f"Docling convert needs weights/network: {result.get('message')}")
    assert result["status"] == "ok"


@pytest.mark.timeout(300)
def test_extract_text_real_tesseract_cli():
    _require_supported_docling()
    if not shutil.which("tesseract"):
        pytest.skip("tesseract CLI not on PATH")
    pytest.importorskip("PIL.Image")
    pytest.importorskip("css_inline")

    result = extract_text(
        _tiny_hello_ocr_png(),
        {"layout_model": "heron", "ocr_backend": "tesseract", "lang": "en"},
    )
    if result.get("status") != "ok" and _is_missing_weight_or_network_error(result):
        pytest.skip(f"Docling convert needs weights/network: {result.get('message')}")
    assert result["status"] == "ok"


_DOCLING_BELOW = "%d.%d.0" % (DOCLING_MIN_VERSION[0], DOCLING_MIN_VERSION[1] - 1)
_DOCLING_FLOOR = "%d.%d.0" % DOCLING_MIN_VERSION


@pytest.mark.parametrize(
    ("dists", "should_raise"),
    [
        ({"docling": _DOCLING_BELOW, "docling-slim": _DOCLING_BELOW}, True),
        ({"docling-slim": _DOCLING_BELOW}, True),
        ({"docling": _DOCLING_FLOOR}, False),
        ({"docling": "2.151.1", "docling-slim": "2.151.1"}, False),
        ({}, False),
    ],
)
def test_check_docling_version(dists, should_raise):
    def fake_version(name: str) -> str:
        if name in dists:
            return dists[name]
        raise importlib.metadata.PackageNotFoundError(name)

    with patch.object(docling_mod.importlib.metadata, "version", side_effect=fake_version):
        if should_raise:
            with pytest.raises(docling_mod.DoclingApiMismatchError) as exc_info:
                docling_mod._check_docling_version()
            assert _DOCLING_BELOW in str(exc_info.value)
            assert f'pip install -U "{DOCLING_REQUIREMENT}"' in str(exc_info.value)
        else:
            docling_mod._check_docling_version()


def test_extract_text_old_docling_reports_upgrade():
    def fake_version(name: str) -> str:
        if name == "docling":
            return _DOCLING_BELOW
        raise importlib.metadata.PackageNotFoundError(name)

    with patch.object(docling_mod.importlib.metadata, "version", side_effect=fake_version):
        result = extract_text(b"png", {})

    assert result["status"] == "error"
    assert result["code"] == "DOCLING_API_MISMATCH"
    assert _DOCLING_BELOW in result["message"]
    assert f'pip install -U "{DOCLING_REQUIREMENT}"' in result["message"]


def test_resolve_layout_model_spec_real_docling():
    _require_supported_docling()
    for key in docling_mod._LAYOUT_KEYS:
        assert docling_mod._resolve_layout_model_spec(key).name == f"layout_{key}"
    for fallback_key in ("egret", "v2", "nope"):
        assert docling_mod._resolve_layout_model_spec(fallback_key).name == "layout_heron"


def test_from_params_rapidocr_options():
    default = docling_mod.DoclingPipelineParams.from_params({})
    assert (default.rapidocr_model_size, default.rapidocr_detect_orientation) == ("small", False)

    custom = docling_mod.DoclingPipelineParams.from_params(
        {"rapidocr_model_size": " Medium ", "rapidocr_detect_orientation": "true"}
    )
    assert (custom.rapidocr_model_size, custom.rapidocr_detect_orientation) == ("medium", True)

    off = docling_mod.DoclingPipelineParams.from_params({"rapidocr_detect_orientation": "off"})
    assert off.rapidocr_detect_orientation is False

    assert docling_mod.DoclingPipelineParams.from_params({"rapidocr_model_size": "medium"}) != default


def test_rapidocr_model_size_not_supported_maps_to_invalid_param():
    class RapidOcrModelSizeNotSupportedError(RuntimeError):
        pass

    err = RapidOcrModelSizeNotSupportedError("RapidOCR has no tiny model for iso:ja-Jpan")
    with patch("plugin.vision.venv.vision_docling._convert_image_bytes", side_effect=err):
        res = extract_text(b"png", {"lang": "ja", "rapidocr_model_size": "tiny"})

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert "RapidOCR has no tiny model" in res["message"]


def test_extract_text_real_docling_tiny_japanese_is_invalid_param():
    _require_supported_docling()
    pytest.importorskip("PIL.Image")
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("css_inline")

    res = extract_text(
        _tiny_hello_ocr_png(),
        {"ocr_backend": "rapidocr", "lang": "ja", "rapidocr_model_size": "tiny"},
    )
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"


@pytest.mark.timeout(300)
def test_extract_text_real_docling_detect_orientation_reads_rotated_page():
    _require_supported_docling()
    pytest.importorskip("PIL.Image")
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("css_inline")

    img_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "ocr_verification_corpus"
        / "10_docling_rotated_90_dense_text.png"
    )
    if not img_path.is_file():
        pytest.skip(f"corpus image missing: {img_path}")

    result = extract_text(
        img_path.read_bytes(),
        {
            "layout_model": "heron",
            "ocr_backend": "rapidocr",
            "lang": "en",
            "rapidocr_detect_orientation": True,
        },
    )
    if result.get("status") != "ok" and _is_missing_weight_or_network_error(result):
        pytest.skip(f"Docling convert needs weights/network: {result.get('message')}")

    assert result["status"] == "ok"
    assert "ZQXPHOENIX" in result["full_text"]


def test_installed_ocr_backend_finds_tesseract_outside_path():
    with patch("shutil.which", side_effect=lambda c: c if c == "/opt/homebrew/bin/tesseract" else None):
        row, program = docling_mod._installed_ocr_backend("tesseract")
    assert row == vision_common_mod.OCR_BACKENDS["tesseract"]
    assert program == "/opt/homebrew/bin/tesseract"


def _make_ocrmac_no_languages_error() -> RuntimeError:
    try:
        try:
            raise AttributeError("VNRecognizeTextRequest")
        except AttributeError as cause:
            raise RuntimeError("Apple Vision did not report any recognition language. Check macOS permissions.") from cause
    except RuntimeError as exc:
        return exc


def _make_nemotron_broken_import_error() -> ImportError:
    try:
        try:
            raise ModuleNotFoundError("No module named 'nemotron_ocr.inference'")
        except ModuleNotFoundError:
            raise ImportError(
                'Nemotron OCR is not installed. Install the optional dependency via `pip install "docling[feat-ocr-nemotron]"`.'
            )
    except ImportError as exc:
        return exc


@pytest.mark.parametrize(
    ("backend_id", "err", "expected_fragments"),
    [
        (
            "tesseract",
            RuntimeError("Tesseract is not available, aborting: too many values to unpack (expected 2) Install tesseract …"),
            ("Install the Tesseract OCR program",),
        ),
        (
            "ocrmac",
            _make_ocrmac_no_languages_error(),
            ("did not report any recognition language", "VNRecognizeTextRequest"),
        ),
        (
            "nemotron",
            RuntimeError("Nemotron OCR requires Python 3.12."),
            ("requires Python 3.12. ", "pip install nemotron-ocr"),
        ),
        (
            "nemotron",
            _make_nemotron_broken_import_error(),
            ("nemotron_ocr.inference", "pip install nemotron-ocr"),
        ),
    ],
    ids=["tesseract", "ocrmac-no-languages", "nemotron-runtime", "nemotron-broken-import"],
)
def test_engine_startup_failure_maps_to_backend_unavailable(backend_id, err, expected_fragments):
    with patch("plugin.vision.venv.vision_docling._convert_image_bytes", side_effect=err):
        res = extract_text(b"png", {"ocr_backend": backend_id})
    assert res["status"] == "error"
    assert res["code"] == "OCR_BACKEND_UNAVAILABLE"
    assert ".." not in res["message"]
    for fragment in expected_fragments:
        assert fragment in res["message"]


@pytest.mark.timeout(300)
def test_extract_text_real_tesseract_cli_off_path(tmp_path, monkeypatch):
    _require_supported_docling()
    real = shutil.which("tesseract")
    if not real:
        pytest.skip("tesseract CLI not installed")
    pytest.importorskip("PIL.Image")
    pytest.importorskip("css_inline")

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setitem(
        vision_common_mod.OCR_BACKENDS,
        "tesseract",
        dataclasses.replace(vision_common_mod.OCR_BACKENDS["tesseract"], programs=("tesseract", real)),
    )
    result = extract_text(
        _tiny_hello_ocr_png(),
        {"layout_model": "heron", "ocr_backend": "tesseract", "lang": "en"},
    )
    if result.get("status") != "ok" and _is_missing_weight_or_network_error(result):
        pytest.skip(f"Docling convert needs weights/network: {result.get('message')}")
    assert result["status"] == "ok"
    assert "hello" in result["full_text"].lower()


@pytest.mark.parametrize("backend_id", ["easyocr", "rapidocr", "ocrmac"])
def test_installed_ocr_backend_broken_import_is_cannot_start(tmp_path, monkeypatch, backend_id):
    if backend_id == "easyocr":
        (tmp_path / "easyocr.py").write_text("import wa_no_such_dep\n", encoding="utf-8")
        expected_reason = "wa_no_such_dep"
        expected_hint = "pip install easyocr"
    elif backend_id == "rapidocr":
        pkg = tmp_path / "rapidocr"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            "def __getattr__(name):\n    raise ImportError('libGL.so.1')\n",
            encoding="utf-8",
        )
        (tmp_path / "onnxruntime.py").write_text("", encoding="utf-8")
        expected_reason = "libGL.so.1"
        expected_hint = "pip install rapidocr onnxruntime"
    else:
        monkeypatch.setattr(sys, "platform", "darwin")
        pkg = tmp_path / "ocrmac"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "ocrmac.py").write_text(
            "raise ModuleNotFoundError(\"No module named 'Vision'\", name='Vision')\n",
            encoding="utf-8",
        )
        expected_reason = "Vision"
        expected_hint = "pip install ocrmac"

    monkeypatch.syspath_prepend(str(tmp_path))
    names = ("easyocr", "wa_no_such_dep", "rapidocr", "onnxruntime", "ocrmac", "ocrmac.ocrmac")
    saved = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    try:
        importlib.invalidate_caches()
        with pytest.raises(docling_mod.OcrBackendError) as exc_info:
            docling_mod._installed_ocr_backend(backend_id)
    finally:
        for n in names:
            sys.modules.pop(n, None)
        sys.modules.update(saved)

    msg = str(exc_info.value)
    assert "cannot start" in msg
    assert expected_reason in msg
    assert expected_hint in msg


class _LocalEntryNotFound(FileNotFoundError, ValueError):
    pass


class DownloadFileException(Exception):
    pass


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("tm_config.json not found"),
        _LocalEntryNotFound("Cannot find the requested files in the disk cache"),
        DownloadFileException("Failed to download https://example.com/model.onnx"),
    ],
)
def test_missing_model_files_map_to_ocr_models_missing(exc):
    res = docling_mod._docling_error(exc, "extract_text")
    assert res["status"] == "error"
    assert res["code"] == "OCR_MODELS_MISSING"
    assert str(exc) in res["message"]


def test_artifacts_path_not_a_folder_is_models_missing(tmp_path):
    missing_dir = tmp_path / "does_not_exist"
    norm = docling_mod.DoclingPipelineParams.from_params({"artifacts_path": str(missing_dir)})
    with pytest.raises(FileNotFoundError) as exc_info:
        docling_mod._apply_pipeline_params(MagicMock(), norm)
    res = docling_mod._docling_error(exc_info.value, "extract_text")
    assert res["code"] == "OCR_MODELS_MISSING"
    assert str(missing_dir) in res["message"]


@pytest.mark.timeout(300)
def test_extract_text_real_docling_empty_artifacts_path(tmp_path):
    _require_supported_docling()
    pytest.importorskip("PIL.Image")
    pytest.importorskip("css_inline")
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")

    result = extract_text(
        _tiny_hello_ocr_png(),
        {"ocr_backend": "rapidocr", "artifacts_path": str(tmp_path)},
    )
    assert result["status"] == "error"
    assert result["code"] == "OCR_MODELS_MISSING"
