# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Threading tests for specialized delegation setup (UNO on main thread)."""

from __future__ import annotations

import threading
from typing import ClassVar
from unittest.mock import MagicMock, patch

from plugin.calc.base import ToolCalcAnalysisBase, ToolCalcSheetBase
from plugin.calc.sheets import ListSheets
from plugin.calc.specialized import DelegateToSpecializedCalc
from plugin.chatbot.smol_agent import SmolToolAdapter
from plugin.contrib.smolagents.memory import FinalAnswerStep
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry
from plugin.framework.worker_pool import run_in_background
from plugin.tests.testing_utils import setup_uno_mocks
from plugin.writer.specialized.footnotes import FootnotesList
from plugin.writer.specialized_base import DelegateToSpecializedWriter, ToolWriterFootnoteBase, ToolWriterShapeBase
from tests.framework.thread_safety import start_uno_thread_safety_session

setup_uno_mocks()


class _DummyAnalysisTool(ToolCalcAnalysisBase):
    name = "dummy_analysis_tool"
    description = "test"
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class _DummyFootnotesList(FootnotesList):
    """Registered footnotes domain tool for writer delegate tests."""

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "footnotes": [], "endnotes": []}

class _DummyShapeTool(ToolBase):
    name = "upsert_shape"
    description = "test"
    parameters = {"type": "object", "properties": {}, "required": []}
    tier = "specialized"
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


# Mark as shapes domain via base mixin pattern used in production tools
class _DummyShapeToolRegistered(_DummyShapeTool, ToolWriterShapeBase):
    pass


class _DummyDocResearchTool(ToolBase):
    name = "grep_nearby_files"
    description = "test"
    parameters = {"type": "object", "properties": {}, "required": []}
    tier = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "matches": []}


@patch("plugin.doc.specialized_base.USE_SUB_AGENT", True)
@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.doc.document_helpers.get_calc_context_for_chat")
def test_calc_delegate_marshals_spreadsheet_context_to_main_thread(
    mock_get_calc_context,
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_int,
):
    mock_get_calc_context.return_value = "Sheets: Sheet1\nActive Sheet: Sheet1"
    mock_execute_on_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    registry = ToolRegistry(MagicMock())
    registry.register(_DummyAnalysisTool())
    registry.register(DelegateToSpecializedCalc())

    mock_doc = MagicMock()
    mock_doc.supportsService.return_value = True

    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.doc = mock_doc
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.stop_checker = lambda: False

    gateway = registry.get("delegate_to_specialized_calc_toolset")
    result = gateway.execute_safe(ctx, domain="analysis", task="Describe sales data")

    assert result["status"] == "ok"
    mock_execute_on_main.assert_called()
    mock_get_calc_context.assert_called_once_with(mock_doc, ctx=ctx.ctx)

    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "[SPREADSHEET CONTEXT]" in instructions
    assert "Sheets: Sheet1" in instructions


@patch("plugin.doc.specialized_base.USE_SUB_AGENT", True)
@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.doc.specialized_base.format_shapes_canvas_context")
def test_writer_delegate_marshals_shapes_canvas_to_main_thread(
    mock_format_canvas,
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_int,
):
    mock_format_canvas.return_value = " Document canvas (Writer): page style 'Standard'; paper 210.0 x 297.0 mm"
    mock_execute_on_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    registry = ToolRegistry(MagicMock())
    registry.register(_DummyShapeToolRegistered())
    registry.register(DelegateToSpecializedWriter())

    mock_doc = MagicMock()
    mock_doc.supportsService.return_value = True

    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.doc = mock_doc
    ctx.ctx = MagicMock()
    ctx.doc_type = "writer"
    ctx.stop_checker = lambda: False

    gateway = registry.get("delegate_to_specialized_writer_toolset")
    result = gateway.execute_safe(ctx, domain="shapes", task="Add a rectangle")

    assert result["status"] == "ok"
    mock_format_canvas.assert_called_once_with(mock_doc)
    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "Document canvas (Writer)" in instructions


@patch("plugin.doc.specialized_base.USE_SUB_AGENT", True)
@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
def test_writer_delegate_marshals_get_tools_to_main_thread(
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_int,
):
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    registry = ToolRegistry(MagicMock())
    registry.register(_DummyFootnotesList())
    registry.register(DelegateToSpecializedWriter())

    mock_doc = MagicMock()
    mock_doc.supportsService.return_value = True
    mock_execute_on_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.doc = mock_doc
    ctx.ctx = MagicMock()
    ctx.doc_type = "writer"
    ctx.stop_checker = lambda: False

    gateway = registry.get("delegate_to_specialized_writer_toolset")
    result = gateway.execute_safe(ctx, domain="footnotes", task="List footnotes")

    assert result["status"] == "ok"
    assert mock_execute_on_main.call_count >= 1


@patch("plugin.doc.specialized_base.USE_SUB_AGENT", True)
@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.doc.document_research.get_open_documents")
@patch("plugin.embeddings.embeddings_indexer.enqueue_folder_index")
def test_writer_delegate_marshals_document_research_scaffolding(
    mock_enqueue_index,
    mock_get_open_docs,
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_int,
):
    mock_get_open_docs.return_value = [{"path": "/tmp/a.odt", "url": "file:///tmp/a.odt", "doc_type": "writer", "is_active": True}]
    mock_execute_on_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    registry = ToolRegistry(MagicMock())
    registry.register(_DummyDocResearchTool())
    registry.register(DelegateToSpecializedWriter())

    mock_doc = MagicMock()
    mock_doc.supportsService.return_value = True

    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.doc = mock_doc
    ctx.ctx = MagicMock()
    ctx.doc_type = "writer"
    ctx.stop_checker = lambda: False

    gateway = registry.get("delegate_to_specialized_writer_toolset")
    result = gateway.execute_safe(ctx, domain="document_research", task="Find budget figures")

    assert result["status"] == "ok"
    mock_enqueue_index.assert_called_once_with(ctx.ctx, ctx.services, mock_doc)
    mock_get_open_docs.assert_called_once_with(ctx.ctx, mock_doc)
    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "[OPEN DOCUMENTS CONTEXT]" in instructions
    assert "/tmp/a.odt" in instructions


def test_writer_smol_adapter_marshals_sync_footnotes_tool():
    """Sync specialized tools must cross execute_on_main_thread via SmolToolAdapter."""
    tool = _DummyFootnotesList()
    tctx = MagicMock()
    tctx.doc_type = "writer"
    adapter = SmolToolAdapter(tool, tctx, safe=True, main_thread_sync=True, inputs_style="specialized")

    with patch("plugin.framework.queue_executor.execute_on_main_thread") as mock_main:
        mock_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        result = adapter.forward()

    assert result["status"] == "ok"
    mock_main.assert_called_once()


def test_writer_footnotes_list_runs_uno_only_on_main_thread():
    """Regression: footnotes_list touches doc UNO; sub-agent worker must marshal before execute."""
    session = start_uno_thread_safety_session()
    try:
        raw_doc = MagicMock()
        raw_doc.supportsService.return_value = True
        notes = MagicMock()
        notes.getCount.return_value = 0
        raw_doc.getFootnotes.return_value = notes
        doc = session.make_mock(raw_doc, name="writer-doc")

        tool = FootnotesList()
        tctx = ToolContext(doc=doc, ctx=MagicMock(), doc_type="writer", services=MagicMock(), caller="test")
        adapter = SmolToolAdapter(tool, tctx, safe=True, main_thread_sync=True, inputs_style="specialized")
        err: AssertionError | None = None
        result: dict | None = None

        def worker():
            nonlocal err, result
            try:
                result = adapter.forward(note_type="footnote")
            except AssertionError as e:
                err = e

        t = run_in_background(worker, name="spec-footnotes", daemon=False)
        t.join(timeout=3.0)
        assert err is None, f"direct UNO from worker: {err}"
        assert result is not None and result.get("status") == "ok"
    finally:
        session.close()


def test_writer_specialized_domain_base_requires_core_read_tools():
    """Writer specialized bases declare get_document_content for sub-agent read helpers."""
    assert "get_document_content" in (ToolWriterFootnoteBase.required_core_tools or frozenset())


def test_calc_sync_tool_marshals_via_smol_adapter():
    """Sync Calc specialized tools must cross execute_on_main_thread via SmolToolAdapter."""
    tool = ListSheets()
    tctx = MagicMock()
    tctx.doc_type = "calc"
    adapter = SmolToolAdapter(tool, tctx, safe=True, main_thread_sync=True, inputs_style="specialized")

    with patch("plugin.framework.queue_executor.execute_on_main_thread") as mock_main:
        mock_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        result = adapter.forward()

    assert result["status"] == "ok"
    mock_main.assert_called_once()


def test_calc_list_sheets_runs_uno_only_on_main_thread():
    """Regression: list_sheets touches doc UNO; sub-agent worker must marshal before execute."""
    session = start_uno_thread_safety_session()
    try:
        raw_sheet = MagicMock()
        raw_sheet.getName.return_value = "Sheet1"
        raw_sheets = MagicMock()
        raw_sheets.getCount.return_value = 1
        raw_sheets.getByIndex.return_value = raw_sheet
        raw_doc = MagicMock()
        raw_doc.supportsService.return_value = True
        raw_doc.getSheets.return_value = raw_sheets
        doc = session.make_mock(raw_doc, name="calc-doc")

        tool = ListSheets()
        tctx = ToolContext(doc=doc, ctx=MagicMock(), doc_type="calc", services=MagicMock(), caller="test")
        adapter = SmolToolAdapter(tool, tctx, safe=True, main_thread_sync=True, inputs_style="specialized")
        err: AssertionError | None = None
        result: dict | None = None

        def worker():
            nonlocal err, result
            try:
                result = adapter.forward()
            except AssertionError as e:
                err = e

        t = run_in_background(worker, name="spec-list-sheets", daemon=False)
        t.join(timeout=3.0)
        assert err is None, f"direct UNO from worker: {err}"
        assert result is not None and result.get("status") == "ok"
        assert result.get("result") == ["Sheet1"]
    finally:
        session.close()


def test_calc_specialized_domain_base_requires_core_read_tools():
    """Calc specialized bases declare sheet read helpers for sub-agent discovery."""
    assert "get_sheet_summary" in (ToolCalcSheetBase.required_core_tools or frozenset())
    assert "read_cell_range" in (ToolCalcSheetBase.required_core_tools or frozenset())


def test_calc_shapes_domain_tools():
    from plugin.calc.shapes import UpsertShape, DeleteShape, ConnectShapes, GroupShapes, GetDrawSummary
    from plugin.calc.base import ToolCalcShapeBase
    
    assert issubclass(UpsertShape, ToolCalcShapeBase)
    assert issubclass(DeleteShape, ToolCalcShapeBase)
    assert issubclass(ConnectShapes, ToolCalcShapeBase)
    assert issubclass(GroupShapes, ToolCalcShapeBase)
    assert issubclass(GetDrawSummary, ToolCalcShapeBase)
    
    assert "com.sun.star.sheet.SpreadsheetDocument" in UpsertShape.uno_services

