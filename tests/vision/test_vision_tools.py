# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for LLM vision OCR tools and availability gating."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.service import ServiceRegistry
from plugin.framework.tool import ToolRegistry
from plugin.vision.vision_availability import (
    filter_vision_delegate_schemas,
    filter_vision_specialized_tools,
    vision_venv_configured,
)
from plugin.vision.vision_tools import ExtractStructureFromImage


@pytest.fixture
def writer_doc():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
    return doc


@pytest.fixture
def calc_doc():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc == "com.sun.star.sheet.SpreadsheetDocument"
    return doc


@pytest.fixture
def tool_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = True
    ctx.ctx = MagicMock()
    ctx.doc_type = "writer"
    return ctx


def _registry_with_vision_tool() -> ToolRegistry:
    from tests.tool_registry_filters import append_production_tool_registry_filters

    services = ServiceRegistry()
    registry = ToolRegistry(services)
    registry.register(ExtractStructureFromImage())
    append_production_tool_registry_filters(registry)
    return registry


def test_extract_structure_not_in_default_core_list(writer_doc):
    registry = _registry_with_vision_tool()
    names = {t.name for t in registry.get_tools(doc=writer_doc)}
    assert "extract_structure_from_image" not in names
    assert "extract_text_from_image" not in names


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True)
def test_extract_structure_in_vision_domain(_mock_avail, writer_doc, calc_doc):
    registry = _registry_with_vision_tool()
    uno_ctx = MagicMock()
    for doc in (writer_doc, calc_doc):
        names = {
            t.name
            for t in registry.get_tools(doc=doc, active_domain="vision", exclude_tiers=(), ctx=uno_ctx)
        }
        assert "extract_structure_from_image" in names
        assert "extract_text_from_image" not in names


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False)
def test_extract_structure_hidden_when_vision_unavailable(_mock_avail, writer_doc):
    registry = _registry_with_vision_tool()
    uno_ctx = MagicMock()
    names = {
        t.name
        for t in registry.get_tools(doc=writer_doc, active_domain="vision", exclude_tiers=(), ctx=uno_ctx)
    }
    assert "extract_structure_from_image" not in names


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True)
def test_delegate_writer_gateway_includes_vision(_mock_avail):
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    gateway = DelegateToSpecializedWriter()
    domains = gateway.parameters["properties"]["domain"]["enum"]
    assert "vision" in domains


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False)
def test_delegate_schema_hides_vision_when_unavailable(_mock_avail):
    schema = {
        "type": "function",
        "function": {
            "name": "delegate_to_specialized_writer_toolset",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "enum": ["images", "vision", "bookmarks"]},
                },
            },
        },
    }
    filtered = filter_vision_delegate_schemas([schema], MagicMock())
    enum = filtered[0]["function"]["parameters"]["properties"]["domain"]["enum"]
    assert "vision" not in enum
    assert "images" in enum


@patch("plugin.vision.vision_availability.resolve_venv_python", return_value="/venv/bin/python")
@patch("plugin.vision.vision_availability.get_config", return_value="/home/user/venv")
def test_vision_venv_configured_true_when_python_resolves(_cfg, _exe):
    assert vision_venv_configured(MagicMock()) is True


@patch("plugin.vision.vision_availability.get_config", return_value="")
def test_vision_venv_configured_false_without_venv_path(_cfg):
    assert vision_venv_configured(MagicMock()) is False


def test_filter_vision_specialized_tools_removes_tool():
    tool = ExtractStructureFromImage()
    other = MagicMock()
    other.name = "specialized_workflow_finished"
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        filtered = filter_vision_specialized_tools([tool, other], MagicMock())
    assert tool not in filtered
    assert other in filtered


@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.vision.vision_tools.run_and_insert_vision_for_selection")
def test_extract_structure_happy_path_inserts(
    mock_run,
    mock_main_thread,
    tool_ctx,
):
    mock_run.return_value = {
        "status": "ok",
        "helper": "extract_structure",
        "full_text": "Hello scan",
        "html": "<p>Hello scan</p>",
        "metrics": {"line_count": 1},
        "warnings": [],
        "inserted": True,
        "images_processed": 1,
        "message": "OCR complete.",
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = ExtractStructureFromImage()
    result = tool.execute(tool_ctx, insert_into_document=True)

    assert result["status"] == "ok"
    assert result["helper"] == "extract_structure"
    assert result["full_text"] == "Hello scan"
    assert result["inserted"] is True
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["helper"] == "extract_structure"
    assert mock_run.call_args.kwargs["insert_into_document"] is True


@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.vision.vision_tools.run_and_insert_vision_for_selection")
def test_extract_structure_return_only_skips_insert(
    mock_run,
    mock_main_thread,
    tool_ctx,
):
    mock_run.return_value = {
        "status": "ok",
        "helper": "extract_structure",
        "full_text": "text only",
        "html": "<p>text only</p>",
        "metrics": {},
        "warnings": [],
        "inserted": False,
        "images_processed": 1,
        "message": "OCR complete (text returned only; not inserted).",
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = ExtractStructureFromImage()
    result = tool.execute(tool_ctx, insert_into_document=False)

    assert result["status"] == "ok"
    assert result["inserted"] is False
    assert mock_run.call_args.kwargs["insert_into_document"] is False


def test_extract_structure_rejects_unsupported_doc(tool_ctx):
    tool_ctx.doc_type = "notebook"
    tool = ExtractStructureFromImage()
    result = tool.execute(tool_ctx)
    assert result["status"] == "error"
    assert "Draw" in result.get("message", "")


@patch("plugin.vision.vision_tools.run_and_insert_vision_for_selection")
def test_extract_structure_draw_omitted_insert_is_return_only(mock_run, tool_ctx):
    mock_run.return_value = {"status": "ok", "full_text": "scan", "inserted": False}
    tool = ExtractStructureFromImage()
    for doc_type in ("draw", "impress"):
        tool_ctx.doc_type = doc_type
        result = tool.execute(tool_ctx)
        assert result["status"] == "ok"
        assert mock_run.call_args.kwargs["insert_into_document"] is False


@patch("plugin.vision.vision_tools.run_and_insert_vision_for_selection")
def test_extract_structure_draw_true_is_vision_error(mock_run, tool_ctx):
    tool_ctx.doc_type = "draw"
    result = ExtractStructureFromImage().execute(tool_ctx, insert_into_document=True)
    assert result["status"] == "error"
    assert result.get("code") == "VISION_ERROR"
    mock_run.assert_not_called()


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True)
@patch.object(ExtractStructureFromImage, "execute", return_value={"status": "ok"})
def test_delegate_draw_vision_passes_insert_false(mock_execute, _avail):
    from plugin.draw.specialized import DelegateToSpecializedDraw

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "draw"
    out = DelegateToSpecializedDraw().execute(ctx, domain="vision", task="")
    assert out["status"] == "ok"
    mock_execute.assert_called_once()
    assert mock_execute.call_args.kwargs["insert_into_document"] is False


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True)
def test_vision_directive_draw_names_draw_gateway(_mock_avail):
    from plugin.framework.doc_type import IMPRESS_DOCUMENT_SERVICE, DRAW_DOCUMENT_SERVICE
    from plugin.vision.vision_availability import get_vision_core_directive

    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc in (DRAW_DOCUMENT_SERVICE, IMPRESS_DOCUMENT_SERVICE)
    text = get_vision_core_directive(doc, MagicMock())
    assert 'delegate_to_specialized_draw_toolset(domain="vision"' in text
    assert "nothing is inserted" in text.lower()


def test_supports_vision_manual_true_for_draw():
    from plugin.vision.vision_runner import supports_vision_manual

    doc = MagicMock()
    with patch("plugin.vision.vision_runner.is_draw", return_value=True):
        assert supports_vision_manual(doc) is True


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False)
def test_get_vision_core_directive_empty_when_unavailable(_mock_avail):
    from plugin.vision.vision_availability import get_vision_core_directive

    assert get_vision_core_directive(MagicMock(), MagicMock()) == ""


@patch("plugin.chatbot.smol_agent.run_inner_tool_agent")
@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True)
@patch.object(ExtractStructureFromImage, "execute", return_value={"status": "ok", "full_text": "hi"})
def test_delegate_vision_runs_ocr_directly(mock_execute, _avail, mock_run_agent):
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "writer"
    gateway = DelegateToSpecializedWriter()
    result = gateway.execute(
        ctx,
        domain="vision",
        task="return the recognized text as plain Unicode",
    )

    assert result["status"] == "ok"
    mock_execute.assert_called_once()
    assert mock_execute.call_args.kwargs["insert_into_document"] is True
    mock_run_agent.assert_not_called()


@patch("plugin.chatbot.smol_agent.run_inner_tool_agent")
@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False)
def test_delegate_vision_unavailable_skips_sub_agent(_avail, mock_run_agent):
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    gateway = DelegateToSpecializedWriter()
    result = gateway.execute(ctx, domain="vision", task="OCR image")

    assert result["status"] == "error"
    assert result.get("code") == "VISION_UNAVAILABLE"
    mock_run_agent.assert_not_called()


def test_delegate_vision_requires_document_lock_when_sub_agent():
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    gateway = DelegateToSpecializedWriter()
    assert gateway.requires_document_lock({"domain": "vision"}) is True


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True)
def test_get_vision_core_directive_calc_when_available(_mock_avail, calc_doc):
    from plugin.vision.vision_availability import get_vision_core_directive

    text = get_vision_core_directive(calc_doc, MagicMock())
    assert 'delegate_to_specialized_calc_toolset(domain="vision"' in text
    assert "domain=\"vision\"" in text
    assert "text and structure" in text
    assert "high-quality" in text
    assert "selected graphic" in text
    assert "task is ignored" in text
    assert "must use this call to perform OCR" in text
    lowered = text.lower()
    for banned in ("html", "docling", "rapidocr", "recognized text"):
        assert banned not in lowered


def test_images_guidance_names_structure_tool():
    from plugin.framework.prompts import WRITER_IMAGES_RULES

    assert "extract_structure_from_image" in WRITER_IMAGES_RULES
    assert "extract_text_from_image" not in WRITER_IMAGES_RULES
    assert "text and structure" in WRITER_IMAGES_RULES.lower()


@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.vision.vision_tools.run_and_insert_vision_for_selection")
def test_extract_structure_partial_failure_details(mock_run, mock_main_thread, tool_ctx):
    mock_run.return_value = {
        "status": "error",
        "code": "VISION_ERROR",
        "helper": "extract_structure",
        "message": "boom",
        "partial": True,
        "inserted": True,
        "images_processed": 1,
        "image_names": ["Img1"],
        "failed_image": "Img2",
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    result = ExtractStructureFromImage().execute(tool_ctx)
    assert result["status"] == "error"
    assert result["code"] == "VISION_ERROR"
    assert result["details"]["partial"] is True
    assert result["details"]["failed_image"] == "Img2"
    assert result["details"]["images_processed"] == 1
    assert result["details"]["image_names"] == ["Img1"]
    assert result["details"]["inserted"] is True


def test_extract_structure_reraises_document_disposed(tool_ctx):
    from plugin.framework.errors import DocumentDisposedError

    with patch("plugin.vision.vision_tools.run_and_insert_vision_for_selection", side_effect=DocumentDisposedError("gone", object_type="vision")):
        with pytest.raises(DocumentDisposedError):
            ExtractStructureFromImage().execute(tool_ctx)


@patch("plugin.vision.vision_tools.run_and_insert_vision_for_selection")
def test_extract_structure_preserves_zero_images_processed(mock_run, tool_ctx):
    mock_run.return_value = {
        "status": "ok",
        "helper": "extract_structure",
        "full_text": "",
        "images_processed": 0,
        "inserted": False,
        "message": "None found",
    }
    result = ExtractStructureFromImage().execute(tool_ctx)
    assert result["status"] == "ok"
    assert result["images_processed"] == 0


@pytest.mark.parametrize(
    ("raw_val", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
    ],
)
@patch("plugin.vision.vision_tools.run_and_insert_vision_for_selection")
def test_extract_structure_coerces_insert_into_document(mock_run, tool_ctx, raw_val, expected):
    mock_run.return_value = {"status": "ok", "images_processed": 1}
    ExtractStructureFromImage().execute(tool_ctx, insert_into_document=raw_val)
    assert mock_run.call_args.kwargs["insert_into_document"] is expected
