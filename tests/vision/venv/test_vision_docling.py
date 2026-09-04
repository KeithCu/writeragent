# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Docling vision backend adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.vision.venv import vision_docling as docling_mod
from plugin.vision.venv.vision_docling import extract_structure, extract_text


class _LayoutModelConfig:
    """Stand-in for docling.datamodel.layout_model_specs.LayoutModelConfig (no get_engine_config)."""

    def __init__(self, name: str, repo_id: str, revision: str = "main"):
        self.name = name
        self.repo_id = repo_id
        self.revision = revision


class _ObjectDetectionModelSpec:
    """Stand-in for docling.datamodel.stage_model_specs.ObjectDetectionModelSpec."""

    def __init__(self, name: str, repo_id: str, revision: str = "main"):
        self.name = name
        self.repo_id = repo_id
        self.revision = revision

    def get_engine_config(self, engine_type=None):
        del engine_type
        return {"repo_id": self.repo_id, "revision": self.revision}


class _LayoutObjectDetectionOptions:
    kind = "layout_object_detection"

    def __init__(self) -> None:
        self.model_spec = _ObjectDetectionModelSpec(
            "layout_heron", "docling-project/docling-layout-heron"
        )
        self.create_orphan_clusters = True

    @classmethod
    def from_preset(cls, preset_id: str):
        spec = _EGRET_OD if preset_id == "layout_egret_large" else _HERON_OD
        return SimpleNamespace(model_spec=spec)


class _LegacyLayoutOptions:
    kind = "docling_layout_default"

    def __init__(self) -> None:
        self.model_spec = _HERON_LEGACY
        self.create_orphan_clusters = True


class _PdfPipelineOptions:
    def __init__(self, **kwargs):
        del kwargs
        self.layout_options = _LayoutObjectDetectionOptions()
        self.ocr_options = None
        self.table_structure_options = SimpleNamespace(mode=None, do_cell_matching=True)
        self.accelerator_options = SimpleNamespace(device="auto", num_threads=4)
        self.images_scale = 1.0
        self.document_timeout = None
        self.artifacts_path = ""
        self.do_formula_enrichment = False
        self.do_code_enrichment = False


class _LegacyPdfPipelineOptions(_PdfPipelineOptions):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout_options = _LegacyLayoutOptions()


_HERON_LEGACY = _LayoutModelConfig("heron", "docling-project/docling-layout-heron")
_EGRET_LEGACY = _LayoutModelConfig("egret_large", "docling-project/docling-layout-egret-large")
_HERON_OD = _ObjectDetectionModelSpec("layout_heron", "docling-project/docling-layout-heron")
_EGRET_OD = _ObjectDetectionModelSpec("layout_egret_large", "docling-project/docling-layout-egret-large")


def _docling_import_side_effect(*, od_layout: bool = True, with_from_preset: bool = True):
    pipeline_mod = MagicMock()
    pipeline_mod.PdfPipelineOptions = _PdfPipelineOptions if od_layout else _LegacyPdfPipelineOptions
    pipeline_mod.RapidOcrOptions = MagicMock(return_value=MagicMock())
    pipeline_mod.TableFormerMode.FAST = "FAST"
    if with_from_preset:
        pipeline_mod.LayoutObjectDetectionOptions = _LayoutObjectDetectionOptions
    else:
        pipeline_mod.LayoutObjectDetectionOptions = None

    layout_specs = MagicMock()
    layout_specs.DOCLING_LAYOUT_HERON = _HERON_LEGACY
    layout_specs.DOCLING_LAYOUT_EGRET_LARGE = _EGRET_LEGACY

    stage_specs = MagicMock()
    stage_specs.ObjectDetectionModelSpec = _ObjectDetectionModelSpec
    stage_specs.OBJECT_DETECTION_LAYOUT_HERON = SimpleNamespace(model_spec=_HERON_OD)
    stage_specs.OBJECT_DETECTION_LAYOUT_EGRET_LARGE = SimpleNamespace(model_spec=_EGRET_OD)

    def side_effect(name: str):
        if name == "docling.datamodel.pipeline_options":
            return pipeline_mod
        if name == "docling.datamodel.layout_model_specs":
            return layout_specs
        if name == "docling.datamodel.stage_model_specs":
            return stage_specs
        return MagicMock()

    return side_effect


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


def test_build_pipeline_options_od_layout_heron_has_get_engine_config():
    """Docling >= 2.118 OD layout_options must keep ObjectDetectionModelSpec (issue 587)."""
    with patch("importlib.import_module", side_effect=_docling_import_side_effect()):
        opts = docling_mod._build_pipeline_options(
            {"ocr_backend": "rapidocr_paddle", "layout_model": "heron"},
            for_structure=True,
        )

    spec = opts.layout_options.model_spec
    assert hasattr(spec, "get_engine_config")
    assert callable(spec.get_engine_config)
    assert isinstance(spec, _ObjectDetectionModelSpec)
    assert spec.name == "layout_heron"


def test_build_pipeline_options_od_layout_egret_large_has_get_engine_config():
    with patch("importlib.import_module", side_effect=_docling_import_side_effect()):
        opts = docling_mod._build_pipeline_options(
            {"ocr_backend": "rapidocr_paddle", "layout_model": "egret_large"},
            for_structure=True,
        )

    spec = opts.layout_options.model_spec
    assert hasattr(spec, "get_engine_config")
    assert callable(spec.get_engine_config)
    assert isinstance(spec, _ObjectDetectionModelSpec)
    assert spec.name == "layout_egret_large"


def test_build_pipeline_options_must_not_leave_layout_model_config_on_od():
    """Today's bug: LayoutModelConfig assigned onto OD options (no get_engine_config)."""
    with patch("importlib.import_module", side_effect=_docling_import_side_effect()):
        opts = docling_mod._build_pipeline_options(
            {"ocr_backend": "rapidocr_paddle", "layout_model": "heron"},
            for_structure=True,
        )

    spec = opts.layout_options.model_spec
    assert not isinstance(spec, _LayoutModelConfig)
    assert hasattr(spec, "get_engine_config")


def test_build_pipeline_options_legacy_layout_keeps_layout_model_config():
    """Docling <= 2.117 LayoutOptions still receives DOCLING_LAYOUT_*."""
    with patch(
        "importlib.import_module",
        side_effect=_docling_import_side_effect(od_layout=False),
    ):
        opts = docling_mod._build_pipeline_options(
            {"ocr_backend": "rapidocr_paddle", "layout_model": "heron"},
            for_structure=True,
        )

    spec = opts.layout_options.model_spec
    assert spec is _HERON_LEGACY
    assert not hasattr(spec, "get_engine_config")


def test_build_pipeline_options_legacy_layout_egret_large():
    with patch(
        "importlib.import_module",
        side_effect=_docling_import_side_effect(od_layout=False),
    ):
        opts = docling_mod._build_pipeline_options(
            {"ocr_backend": "rapidocr_paddle", "layout_model": "egret_large"},
            for_structure=True,
        )

    assert opts.layout_options.model_spec is _EGRET_LEGACY


def test_apply_pipeline_params_od_path_never_assigns_layout_model_config():
    layout_opts = _LayoutObjectDetectionOptions()
    pipeline = SimpleNamespace(
        layout_options=layout_opts,
        table_structure_options=SimpleNamespace(),
        accelerator_options=SimpleNamespace(),
    )

    with patch("importlib.import_module", side_effect=_docling_import_side_effect()):
        docling_mod._apply_pipeline_params(
            pipeline,
            {"layout_model": "egret_large"},
            for_structure=True,
        )

    assert isinstance(layout_opts.model_spec, _ObjectDetectionModelSpec)
    assert not isinstance(layout_opts.model_spec, _LayoutModelConfig)
    assert hasattr(layout_opts.model_spec, "get_engine_config")
    assert layout_opts.model_spec.name == "layout_egret_large"

