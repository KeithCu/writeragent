# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Threading tests for specialized delegation setup (UNO on main thread)."""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.base import ToolCalcSpecialBase
from plugin.calc.sheets import ListSheets
from plugin.calc.specialized import DelegateToSpecializedCalc
from plugin.chatbot.smol_agent import SmolToolAdapter
from plugin.contrib.smolagents.memory import FinalAnswerStep
from plugin.doc.peer_message import SendPeerResult, SendPeerWork
from plugin.framework.errors import DocumentDisposedError
from plugin.framework import thread_guard as tg
from plugin.doc.peer_prompt import get_peer_inner_choice_block
from plugin.framework.prompts import get_document_research_workflow_hint
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry
from plugin.framework.uno_context import get_runtime_uid
from plugin.framework.worker_pool import run_in_background
from plugin.writer.specialized.footnotes import FootnotesList
from plugin.writer.specialized_base import DelegateToSpecializedWriter, ToolWriterShapeBase
from tests.framework.thread_safety import start_uno_thread_safety_session


@pytest.fixture(autouse=True)
def _register_prompt_hooks_for_delegation_threading_tests():
    from plugin.writeragent_hooks import register_writeragent_prompt_hooks

    register_writeragent_prompt_hooks()


class _DummyAnalysisBase(ToolCalcSpecialBase):
    # Domain-less intermediate base: a direct ToolCalcSpecialBase subclass with a
    # domain would leak "analysis" into the discovery catalog for the whole test run.
    specialized_domain: ClassVar[str | None] = None
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(["get_sheet_summary"])


class _DummyAnalysisTool(_DummyAnalysisBase):
    specialized_domain = "analysis"
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
    name = "shape_upsert"
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


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analyzer.get_calc_context_for_chat")
def test_calc_delegate_marshals_spreadsheet_context_to_main_thread(
    mock_get_calc_context,
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_val,
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

    assert result["status"] == "incomplete"
    mock_execute_on_main.assert_called()
    mock_get_calc_context.assert_called_once_with(mock_doc, ctx=ctx.ctx)

    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "[SPREADSHEET CONTEXT]" in instructions
    assert "Sheets: Sheet1" in instructions


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analyzer.get_calc_context_for_chat")
def test_calc_delegate_disposed_context_reports_document_disposed(
    mock_get_calc_context,
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_val,
):
    mock_get_calc_context.side_effect = DocumentDisposedError("doc closed")
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
    assert gateway is not None
    result = gateway.execute_safe(ctx, domain="analysis", task="Describe sales data")

    assert result["status"] == "error"
    assert result["code"] == "DOCUMENT_DISPOSED"


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.calc.analyzer.get_calc_context_for_chat")
def test_calc_delegate_guard_proxied_doc_presence_is_identity_safe(
    mock_get_calc_context,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_val,
):
    """Regression: truthiness on guard-proxied ctx.doc trips UNO bool on MCP/long-running worker."""
    mock_get_calc_context.return_value = "Sheets: Sheet1\nActive Sheet: Sheet1"
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    registry = ToolRegistry(MagicMock())
    registry.register(_DummyAnalysisTool())
    registry.register(DelegateToSpecializedCalc())

    raw_doc = MagicMock()
    raw_doc.supportsService.return_value = True
    # MagicMock bypasses guard_uno(); wrap explicitly so __bool__ asserts main thread.
    proxied_doc = tg._UnoThreadGuardProxy(raw_doc)

    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.doc = proxied_doc
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.stop_checker = lambda: False

    gateway = registry.get("delegate_to_specialized_calc_toolset")
    session = start_uno_thread_safety_session()
    was_guard = tg.GUARD_ON
    tg.GUARD_ON = True
    err: BaseException | None = None
    result: dict | None = None
    try:
        # Avoid modal MessageBox if a violation fires during the run.
        with patch.object(tg, "_notify_thread_violation"):

            def worker():
                nonlocal err, result
                try:
                    result = gateway.execute_safe(ctx, domain="analysis", task="Describe sales data")
                except BaseException as e:
                    err = e

            t = run_in_background(worker, name="calc-delegate-uno-bool", daemon=False)
            t.join(timeout=5.0)
    finally:
        tg.GUARD_ON = was_guard
        session.close()

    assert err is None, f"UNO touch from worker: {err}"
    assert result is not None and result.get("status") == "incomplete"
    mock_get_calc_context.assert_called_once()
    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "[SPREADSHEET CONTEXT]" in instructions


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.doc.specialized_base.format_shapes_canvas_context")
def test_writer_delegate_marshals_shapes_canvas_to_main_thread(
    mock_format_canvas,
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_val,
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

    assert result["status"] == "incomplete"
    mock_format_canvas.assert_called_once_with(mock_doc)
    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "Document canvas (Writer)" in instructions


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.main.call")
def test_writer_delegate_marshals_get_tools_to_main_thread(
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_val,
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

    assert result["status"] == "incomplete"


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.main.call")
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
    _mock_get_config_val,
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

    assert result["status"] == "incomplete"
    mock_enqueue_index.assert_called_once_with(ctx.ctx, mock_doc)
    # Open-docs context plus list_v1_peers (peer catalog / inner ASK vs REPLY hint),
    # both gathered on the main thread with get_tools.
    assert mock_get_open_docs.call_count >= 1
    mock_get_open_docs.assert_called_with(ctx.ctx, mock_doc)
    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "[OPEN DOCUMENTS CONTEXT]" in instructions
    assert "/tmp/a.odt" in instructions


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.doc.document_research.get_open_documents")
@patch("plugin.embeddings.embeddings_indexer.enqueue_folder_index")
def test_writer_delegate_disposed_open_documents_reports_document_disposed(
    mock_enqueue_index,
    mock_get_open_docs,
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_val,
):
    mock_get_open_docs.side_effect = DocumentDisposedError("doc closed")
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
    assert gateway is not None
    result = gateway.execute_safe(ctx, domain="document_research", task="Find budget figures")

    assert result["status"] == "error"
    assert result["code"] == "DOCUMENT_DISPOSED"


_PEER_HINT_PEERS = [{"name": "Budget.ods", "uid": "u2", "url": "", "type": "calc"}]


def _list_v1_peers_touching_runtime_uid(uno_ctx, self_doc):
    """Stand-in for list_v1_peers that reproduces the #673 UNO touch."""
    get_runtime_uid(self_doc)
    return list(_PEER_HINT_PEERS)


def test_document_research_hint_off_main_does_not_touch_runtime_uid():
    """Regression: get_peer_inner_choice_block called getRuntimeUID on the specialize worker."""
    session = start_uno_thread_safety_session()
    was_guard = tg.GUARD_ON
    tg.GUARD_ON = True
    err: BaseException | None = None
    hint: str | None = None
    inner: str | None = None
    try:
        raw_doc = MagicMock()
        raw_doc.getRuntimeUID.return_value = "uid-self"
        doc = session.make_mock(raw_doc, name="writer-doc")
        uno_ctx = MagicMock()

        def worker():
            nonlocal err, hint, inner
            try:
                with patch(
                    "plugin.doc.peer_message.list_v1_peers",
                    side_effect=_list_v1_peers_touching_runtime_uid,
                ):
                    hint = get_document_research_workflow_hint(uno_ctx, doc)
                    inner = get_peer_inner_choice_block(uno_ctx, doc)
            except BaseException as e:
                err = e

        t = run_in_background(
            worker, name="tool-async-delegate_to_specialized_writer_toolset", daemon=False
        )
        t.join(timeout=5.0)
    finally:
        tg.GUARD_ON = was_guard
        session.close()

    assert err is None, f"UNO touch from worker: {err}"
    assert inner and inner in hint


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.doc.document_research.get_open_documents")
@patch("plugin.embeddings.embeddings_indexer.enqueue_folder_index")
def test_document_research_delegate_off_main_does_not_touch_runtime_uid(
    mock_enqueue_index,
    mock_get_open_docs,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_val,
):
    """Regression: specialized execute built the #673 peer catalog on the async worker.

    Writer/Calc/Draw gateways share DelegateToSpecializedBase.execute.
    """
    mock_get_open_docs.return_value = []
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    registry = ToolRegistry(MagicMock())
    registry.register(_DummyDocResearchTool())
    registry.register(SendPeerWork())
    registry.register(SendPeerResult())
    registry.register(DelegateToSpecializedWriter())

    session = start_uno_thread_safety_session()
    was_guard = tg.GUARD_ON
    tg.GUARD_ON = True
    err: BaseException | None = None
    result: dict | None = None
    try:
        raw_doc = MagicMock()
        raw_doc.supportsService.return_value = True
        raw_doc.getRuntimeUID.return_value = "uid-self"
        proxied_doc = tg._UnoThreadGuardProxy(raw_doc)

        ctx = MagicMock()
        ctx.services = {"tools": registry}
        ctx.doc = proxied_doc
        ctx.ctx = MagicMock()
        ctx.doc_type = "writer"
        ctx.stop_checker = lambda: False
        ctx.active_domain = None

        gateway = registry.get("delegate_to_specialized_writer_toolset")
        with patch.object(tg, "_notify_thread_violation"):
            with patch(
                "plugin.doc.peer_message.list_v1_peers",
                side_effect=_list_v1_peers_touching_runtime_uid,
            ):

                def worker():
                    nonlocal err, result
                    try:
                        result = gateway.execute_safe(
                            ctx, domain="document_research", task="Get Q4 from the open budget"
                        )
                    except BaseException as e:
                        err = e

                t = run_in_background(
                    worker,
                    name="tool-async-delegate_to_specialized_writer_toolset",
                    daemon=False,
                )
                t.join(timeout=5.0)
    finally:
        tg.GUARD_ON = was_guard
        session.close()

    assert err is None, f"UNO touch from worker: {err}"
    assert result is not None and result.get("status") == "incomplete"
    smol_tools = mock_agent_class.call_args.kwargs.get("tools", [])
    names = {t.name for t in smol_tools}
    assert "send_peer_work" in names
    assert "send_peer_result" in names
    mock_enqueue_index.assert_called_once()


def test_writer_footnotes_list_runs_uno_only_when_current():
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
        adapter = SmolToolAdapter(tool, tctx, safe=True, inputs_style="specialized")
        err: AssertionError | None = None
        result: dict | None = None

        def worker():
            nonlocal err, result
            try:
                result = adapter.forward(note="footnote")
            except AssertionError as e:
                err = e

        t = run_in_background(worker, name="spec-footnotes", daemon=False)
        t.join(timeout=3.0)
        assert err is None, f"direct UNO from worker: {err}"
        assert result is not None and result.get("status") == "ok"
    finally:
        session.close()


def test_calc_list_sheets_runs_uno_only_when_current():
    """Regression: sheet_list touches doc UNO; sub-agent worker must marshal before execute."""
    session = start_uno_thread_safety_session()
    try:
        raw_sheet = MagicMock()
        raw_sheet.getName.return_value = "Sheet1"
        raw_sheets = MagicMock()
        raw_sheets.getCount.return_value = 1
        raw_sheets.getByIndex.return_value = raw_sheet
        raw_sheets.getElementNames.return_value = ("Sheet1",)
        list_sheet = MagicMock()
        list_sheet.isProtected.return_value = False
        list_sheet.getPropertyValue.return_value = True
        raw_sheets.getByName.return_value = list_sheet
        raw_doc = MagicMock()
        raw_doc.supportsService.return_value = True
        raw_doc.getSheets.return_value = raw_sheets
        doc = session.make_mock(raw_doc, name="calc-doc")

        tool = ListSheets()
        tctx = ToolContext(doc=doc, ctx=MagicMock(), doc_type="calc", services=MagicMock(), caller="test")
        adapter = SmolToolAdapter(tool, tctx, safe=True, inputs_style="specialized")
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
        assert result.get("result") == [{"name": "Sheet1", "protected": False, "visible": True}]
    finally:
        session.close()


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analyzer.get_calc_context_for_chat")
def test_calc_sheets_delegate_forwards_create_instruction_to_outer(
    mock_get_calc_context,
    mock_execute_on_main,
    _mock_llm,
    _mock_smol_model,
    mock_agent_class,
    _mock_get_config,
    _mock_get_config_val,
):
    """Outer hop payload gets web-research-style `instruction` after sheet_create."""
    from plugin.calc.sheets import CreateSheet
    from plugin.contrib.smolagents.memory import ToolCall
    from plugin.framework.prompts import SHEETS_CREATED_NOT_POPULATED_INSTRUCTION

    mock_get_calc_context.return_value = "Sheets: Sheet1\nActive Sheet: Sheet1"
    mock_execute_on_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    mock_agent_class.return_value = MagicMock()

    registry = ToolRegistry(MagicMock())
    registry.register(CreateSheet())
    registry.register(DelegateToSpecializedCalc())

    mock_doc = MagicMock()
    mock_doc.supportsService.return_value = True

    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.doc = mock_doc
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.stop_checker = lambda: False

    def fake_execute_safe(agent, task, tool_call_handler=None, **kwargs):
        if tool_call_handler:
            tool_call_handler(ToolCall(name="sheet_create", arguments={"sheet": "Q1 Actuals"}, id="1"))
        return {
            "status": "ok",
            "finished": True,
            "answer": "Created Q1 Actuals",
            "message": "Specialized task complete.",
        }

    gateway = registry.get("delegate_to_specialized_calc_toolset")
    with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_executor_cls:
        mock_executor_cls.return_value.execute_safe.side_effect = fake_execute_safe
        result = gateway.execute_safe(ctx, domain="sheets", task="Create a sheet named Q1 Actuals")

    assert result["status"] == "ok"
    assert result["instruction"] == SHEETS_CREATED_NOT_POPULATED_INSTRUCTION


def test_calc_shapes_domain_tools():
    from plugin.calc.shapes import UpsertShape, DeleteShape, ConnectShapes, GroupShapes, GetDrawSummary
    from plugin.framework.tool import ToolRegistry

    services = MagicMock()
    reg = ToolRegistry(services)
    reg.register(UpsertShape())
    reg.register(DeleteShape())
    reg.register(ConnectShapes())
    reg.register(GroupShapes())
    reg.register(GetDrawSummary())

    doc = MagicMock()
    doc.supportsService.return_value = True

    tools = reg.get_tools(doc=doc, active_domain="shapes", exclude_tiers=())
    names = {t.name for t in tools}
    assert "shape_upsert" in names
    assert "shape_delete" in names
    assert "shape_connect" in names
    assert "shape_group" in names
    assert "shape_summary" in names


def test_outer_turn_is_peer_work_probe_failure_returns_false():
    """When _probe raises (e.g. CancelledError with empty str), fail closed to False."""
    from plugin.doc.specialized_base import _outer_turn_is_peer_work

    class EmptyMessageError(Exception):
        def __str__(self):
            return ""

    with patch("plugin.framework.queue_executor.main.call", side_effect=EmptyMessageError):
        res = _outer_turn_is_peer_work(MagicMock())

    assert res is False


def test_outer_turn_is_peer_work_active_query_text_returns_true():
    from plugin.doc.specialized_base import _outer_turn_is_peer_work
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    panel = MagicMock()
    listener = MagicMock()
    panel.send_listener = listener
    turn = begin_send_turn(listener)
    turn.active_query_text = "[Peer work from: writer_uid] Please format table"

    with patch("plugin.doc.specialized_base.get_runtime_uid", return_value="doc_uid_123"), \
         patch("plugin.doc.specialized_base.get_live_panel", return_value=panel), \
         patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()):
        res = _outer_turn_is_peer_work(ctx)

    assert res is True
