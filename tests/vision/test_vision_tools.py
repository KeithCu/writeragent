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
from plugin.tests.testing_utils import setup_uno_mocks
from plugin.vision.vision_availability import (
    filter_vision_delegate_schemas,
    filter_vision_specialized_tools,
    invalidate_vision_availability_cache,
    vision_ocr_available,
)
from plugin.vision.vision_tools import ExtractTextFromImage

setup_uno_mocks()


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
    services = ServiceRegistry()
    registry = ToolRegistry(services)
    registry.register(ExtractTextFromImage())
    return registry


def test_extract_text_not_in_default_core_list(writer_doc):
    registry = _registry_with_vision_tool()
    names = {t.name for t in registry.get_tools(doc=writer_doc)}
    assert "extract_text_from_image" not in names


@patch("plugin.vision.vision_availability.vision_ocr_available", return_value=True)
def test_extract_text_in_vision_domain(_mock_avail, writer_doc, calc_doc):
    registry = _registry_with_vision_tool()
    uno_ctx = MagicMock()
    for doc in (writer_doc, calc_doc):
        names = {
            t.name
            for t in registry.get_tools(doc=doc, active_domain="vision", exclude_tiers=(), ctx=uno_ctx)
        }
        assert "extract_text_from_image" in names


@patch("plugin.vision.vision_availability.vision_ocr_available", return_value=False)
def test_extract_text_hidden_when_vision_unavailable(_mock_avail, writer_doc):
    registry = _registry_with_vision_tool()
    uno_ctx = MagicMock()
    names = {
        t.name
        for t in registry.get_tools(doc=writer_doc, active_domain="vision", exclude_tiers=(), ctx=uno_ctx)
    }
    assert "extract_text_from_image" not in names


@patch("plugin.vision.vision_availability.vision_ocr_available", return_value=True)
def test_delegate_writer_gateway_includes_vision(_mock_avail):
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    gateway = DelegateToSpecializedWriter()
    domains = gateway.parameters["properties"]["domain"]["enum"]
    assert "vision" in domains


@patch("plugin.vision.vision_availability.vision_ocr_available", return_value=False)
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


@patch("plugin.vision.vision_availability._probe_ready", return_value=True)
@patch("plugin.vision.vision_availability._resolve_vision_python_exe", return_value="/venv/bin/python")
@patch("plugin.framework.config.get_config_str", return_value="/home/user/venv")
def test_vision_ocr_available_true_when_probe_ready(_cfg, _exe, _probe):
    invalidate_vision_availability_cache()
    assert vision_ocr_available(MagicMock()) is True


@patch("plugin.framework.config.get_config_str", return_value="")
def test_vision_ocr_available_false_without_venv_path(_cfg):
    invalidate_vision_availability_cache()
    assert vision_ocr_available(MagicMock()) is False


@patch("plugin.vision.vision_availability._probe_ready", return_value=False)
@patch("plugin.vision.vision_availability._resolve_vision_python_exe", return_value="/venv/bin/python")
@patch("plugin.framework.config.get_config_str", return_value="/home/user/venv")
def test_vision_ocr_available_false_when_probe_fails(_cfg, _exe, _probe):
    invalidate_vision_availability_cache()
    assert vision_ocr_available(MagicMock()) is False


def test_filter_vision_specialized_tools_removes_tool():
    tool = ExtractTextFromImage()
    other = MagicMock()
    other.name = "specialized_workflow_finished"
    with patch("plugin.vision.vision_availability.vision_ocr_available", return_value=False):
        filtered = filter_vision_specialized_tools([tool, other], MagicMock())
    assert tool not in filtered
    assert other in filtered


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.vision.vision_tools.run_trusted_vision")
@patch("plugin.vision.vision_tools.insert_vision_result")
def test_extract_text_happy_path_inserts(
    mock_insert,
    mock_run,
    mock_main_thread,
    tool_ctx,
):
    mock_run.return_value = {
        "status": "ok",
        "helper": "extract_text",
        "full_text": "Hello scan",
        "html": "<p>Hello scan</p>",
        "metrics": {"line_count": 1},
        "warnings": [],
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = ExtractTextFromImage()
    result = tool.execute(tool_ctx, insert_into_document=True)

    assert result["status"] == "ok"
    assert result["full_text"] == "Hello scan"
    assert result["inserted"] is True
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["helper"] == "extract_text"
    mock_insert.assert_called_once()


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.vision.vision_tools.run_trusted_vision")
@patch("plugin.vision.vision_tools.insert_vision_result")
def test_extract_text_return_only_skips_insert(
    mock_insert,
    mock_run,
    mock_main_thread,
    tool_ctx,
):
    mock_run.return_value = {
        "status": "ok",
        "helper": "extract_text",
        "full_text": "text only",
        "html": "<p>text only</p>",
        "metrics": {},
        "warnings": [],
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = ExtractTextFromImage()
    result = tool.execute(tool_ctx, insert_into_document=False)

    assert result["status"] == "ok"
    assert result["inserted"] is False
    mock_insert.assert_not_called()


def test_extract_text_rejects_unsupported_doc(tool_ctx):
    tool_ctx.doc_type = "draw"
    tool = ExtractTextFromImage()
    result = tool.execute(tool_ctx)
    assert result["status"] == "error"
    assert "Writer or Calc" in result.get("message", "")


@patch("plugin.vision.vision_availability.vision_ocr_available", return_value=False)
def test_get_vision_core_directive_empty_when_unavailable(_mock_avail):
    from plugin.framework.constants import get_vision_core_directive

    assert get_vision_core_directive(MagicMock(), MagicMock()) == ""


@patch("plugin.doc.specialized_base.USE_SUB_AGENT", True)
@patch("plugin.doc.specialized_base.build_toolcalling_agent")
@patch("plugin.vision.vision_availability.vision_ocr_available", return_value=True)
@patch.object(ExtractTextFromImage, "execute", return_value={"status": "ok", "full_text": "hi"})
def test_delegate_vision_runs_ocr_directly(mock_execute, _avail, mock_build_agent):
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
    mock_build_agent.assert_not_called()


@patch("plugin.doc.specialized_base.USE_SUB_AGENT", True)
@patch("plugin.doc.specialized_base.build_toolcalling_agent")
@patch("plugin.vision.vision_availability.vision_ocr_available", return_value=False)
def test_delegate_vision_unavailable_skips_sub_agent(_avail, mock_build_agent):
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    gateway = DelegateToSpecializedWriter()
    result = gateway.execute(ctx, domain="vision", task="OCR image")

    assert result["status"] == "error"
    assert result.get("code") == "VISION_UNAVAILABLE"
    mock_build_agent.assert_not_called()


def test_delegate_vision_no_document_lock():
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    gateway = DelegateToSpecializedWriter()
    assert gateway.requires_document_lock({"domain": "vision"}) is False


@patch("plugin.vision.vision_availability.vision_ocr_available", return_value=True)
def test_get_vision_core_directive_when_available(_mock_avail, writer_doc):
    from plugin.framework.constants import get_vision_core_directive

    text = get_vision_core_directive(writer_doc, MagicMock())
    assert "domain=\"vision\"" in text
    assert "selected graphic" in text
    assert "task is ignored" in text
    assert "must use this call to perform OCR" in text
