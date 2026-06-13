# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Tests for document_research delegation and read-only enforcement."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.specialized import DelegateToSpecializedCalc
from plugin.contrib.smolagents.memory import FinalAnswerStep, ToolCall
from plugin.doc.document_research_specialized import DelegateReadDocument, run_inner_read_agent
from plugin.doc.specialized_base import DelegateToSpecializedBase
from plugin.draw.specialized import DelegateToSpecializedDraw
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry
from tests.chatbot.test_tool_loop import _mock_get_config_int_for_sub_agent
from plugin.writer.specialized_base import DelegateToSpecializedWriter, SpecializedWorkflowFinished
from plugin.embeddings.document_research_fts_tool import SearchNearbyFiles
from plugin.doc.document_research_grep_tool import GrepNearbyFiles
from plugin.doc.document_research_tools import ListNearbyFiles


class _MutatingTool(ToolBase):
    name = "apply_fake"
    description = "test mutator"
    is_mutation = True
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


def _document_research_domains(gateway):
    return gateway.parameters["properties"]["domain"]["enum"]


def test_document_research_in_writer_delegate_enum():
    gw = DelegateToSpecializedWriter()
    assert "document_research" in _document_research_domains(gw)


def test_document_research_in_calc_delegate_enum():
    gw = DelegateToSpecializedCalc()
    assert "document_research" in _document_research_domains(gw)


def test_document_research_in_draw_delegate_enum():
    gw = DelegateToSpecializedDraw()
    assert "document_research" in _document_research_domains(gw)


def test_filter_document_research_discovery_tools_respects_config():
    from plugin.doc.document_research import filter_document_research_discovery_tools

    r = ToolRegistry(services={})
    r.register(ListNearbyFiles())
    r.register(GrepNearbyFiles())
    r.register(SearchNearbyFiles())
    r.register(DelegateReadDocument())
    r.register(SpecializedWorkflowFinished())
    tools = r.get_tools(doc=MagicMock(), active_domain="document_research", exclude_tiers=())
    ctx = MagicMock()

    with patch("plugin.framework.constants.folder_search_enabled", return_value=False):
        filtered = filter_document_research_discovery_tools(tools, ctx)
    names = {t.name for t in filtered}
    assert "list_nearby_files" in names
    assert "grep_nearby_files" in names
    assert "delegate_read_document" in names
    assert "specialized_workflow_finished" in names
    assert "search_embeddings" not in names
    assert "search_nearby_files" not in names

    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        filtered = filter_document_research_discovery_tools(tools, ctx)
    names = {t.name for t in filtered}
    assert "search_nearby_files" in names
    assert "search_embeddings" not in names
    assert "grep_nearby_files" not in names


def test_document_research_workflow_hint_off():
    from plugin.doc.document_research import get_document_research_workflow_hint

    ctx = MagicMock()
    with patch("plugin.framework.constants.folder_search_enabled", return_value=False):
        hint = get_document_research_workflow_hint(ctx)
    assert "grep_nearby_files" in hint
    assert "search_embeddings" not in hint
    assert "search_nearby_files" not in hint
    assert "fused" not in hint


def test_document_research_workflow_hint_on():
    from plugin.doc.document_research import get_document_research_workflow_hint

    ctx = MagicMock()
    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        hint = get_document_research_workflow_hint(ctx)
    assert "search_nearby_files" in hint
    assert "search_embeddings" not in hint
    assert "grep_nearby_files" not in hint
    assert "fused" in hint

@patch("plugin.doc.specialized_base.USE_SUB_AGENT", False)
def test_document_research_requires_sub_agent_when_disabled():
    r = ToolRegistry(services={})
    r.register(DelegateToSpecializedWriter())
    gw = r.get("delegate_to_specialized_writer_toolset")
    ctx = MagicMock()
    ctx.services = {"tools": r}
    result = gw.execute_safe(ctx, domain="document_research", task="read budget")
    assert result["status"] == "error"
    assert "DOCUMENT_RESEARCH_REQUIRES_SUB_AGENT" in result.get("code", "") or "specialized task" in result.get("message", "").lower()


def test_read_only_target_blocks_mutation_in_registry():
    r = ToolRegistry(services={})
    r.register(_MutatingTool())
    doc = MagicMock()
    doc.supportsService = lambda svc: svc == "com.sun.star.text.TextDocument"
    ctx = ToolContext(doc, MagicMock(), "writer", r, read_only_target=True)
    result = r.execute("apply_fake", ctx)
    assert result["status"] == "error"
    assert result.get("code") == "READ_ONLY_TARGET"


@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_document_research_outer_delegation_gets_document_research_tools(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
):
    r = ToolRegistry(services={})
    r.register(ListNearbyFiles())
    r.register(GrepNearbyFiles())
    r.register(DelegateReadDocument())
    r.register(SpecializedWorkflowFinished())
    r.register(DelegateToSpecializedWriter())

    mock_get_config.return_value = {}
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService = lambda svc: svc == "com.sun.star.text.TextDocument"
    ctx.ctx = MagicMock()
    ctx.services = {"tools": r}
    ctx.stop_checker = lambda: False

    gw = r.get("delegate_to_specialized_writer_toolset")
    with patch("plugin.doc.document_research.get_open_documents", return_value=[]):
        result = gw.execute_safe(ctx, domain="document_research", task="Find Q4 in budget")
    assert result["status"] == "ok"
    smol_tools = mock_agent_class.call_args.kwargs.get("tools", [])
    names = {t.name for t in smol_tools}
    assert "list_nearby_files" in names
    assert "grep_nearby_files" in names
    assert "delegate_read_document" in names


@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.doc.specialized_base.SmolAgentExecutor")
def test_document_research_chat_append_on_delegate_read_document(
    mock_executor_cls,
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
):
    captured: list[str] = []

    def fake_execute_safe(agent, task, tool_call_handler=None, **kwargs):
        if tool_call_handler:
            tool_call_handler(
                ToolCall(
                    name="delegate_read_document",
                    arguments={"path_or_name": "/tmp/Budget.ods", "task": "Q4"},
                    id="test-call-1",
                )
            )
        return "done"

    mock_executor_cls.return_value.execute_safe.side_effect = fake_execute_safe

    r = ToolRegistry(services={})
    r.register(ListNearbyFiles())
    r.register(DelegateReadDocument())
    r.register(SpecializedWorkflowFinished())
    r.register(DelegateToSpecializedWriter())

    mock_get_config.return_value = {}
    mock_agent_class.return_value = MagicMock()

    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService = lambda svc: svc == "com.sun.star.text.TextDocument"
    ctx.ctx = MagicMock()
    ctx.services = {"tools": r}
    ctx.stop_checker = lambda: False
    ctx.chat_append_callback = captured.append

    gw = r.get("delegate_to_specialized_writer_toolset")
    with patch("plugin.doc.document_research.get_open_documents", return_value=[]):
        result = gw.execute_safe(ctx, domain="document_research", task="Find Q4 in budget")
    assert result["status"] == "ok"
    assert len(captured) == 1
    assert "Tool: delegate_read_document" in captured[0]
    assert "Budget.ods" in captured[0]
    assert "[Document research]" not in captured[0]
    assert "delegate_read_document" in captured[0]


@patch("plugin.doc.document_research_specialized.build_toolcalling_agent")
@patch("plugin.doc.document_research_specialized.SmolAgentExecutor")
def test_run_inner_read_agent_uses_allowlist(mock_executor_cls, mock_build_agent):
    mock_agent = MagicMock()
    mock_build_agent.return_value = mock_agent
    mock_executor = MagicMock()
    mock_executor.execute_safe.return_value = "Q4=100"
    mock_executor_cls.return_value = mock_executor

    r = ToolRegistry(services={})
    from plugin.calc.cells import ReadCellRange
    from plugin.calc.sheets import GetSheetSummary

    r.register(ReadCellRange())
    r.register(GetSheetSummary())
    r.register(SpecializedWorkflowFinished())

    doc = MagicMock()
    doc.supportsService = lambda svc: svc == "com.sun.star.sheet.SpreadsheetDocument"
    parent = ToolContext(doc, MagicMock(), "calc", {"tools": r}, stop_checker=lambda: False)

    opened = MagicMock()
    opened.supportsService = doc.supportsService
    result = run_inner_read_agent(parent, opened, "calc", "extract Q4")
    assert result == "Q4=100"
    tools_arg = mock_build_agent.call_args[0][1]
    tool_names = [t.name for t in tools_arg]
    assert "read_cell_range" in tool_names
    assert "get_sheet_summary" in tool_names
    inner_ctx = mock_build_agent.call_args[0][0]
    assert inner_ctx.read_only_target is True


@patch("plugin.doc.document_research_specialized.run_inner_read_agent", return_value={"status": "ok", "result": "42"})
@patch("plugin.doc.document_research_specialized.close_document_research_document")
@patch("plugin.doc.document_research_specialized.open_document_for_read")
@patch("plugin.doc.document_research_specialized.resolve_path_or_name")
@patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn, *a, **k: fn())
def test_delegate_read_document_does_not_use_delegate_gateway(
    mock_main,
    mock_resolve,
    mock_open,
    mock_close,
    mock_inner,
):
    opened_model = MagicMock()
    mock_resolve.return_value = ("/tmp/Budget.ods", "file:///tmp/Budget.ods")
    mock_open.return_value = (opened_model, "calc", None, True)

    r = ToolRegistry(services={})
    r.register(DelegateReadDocument())
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.services = {"tools": r}

    tool = r.get("delegate_read_document")
    result = tool.execute_safe(ctx, path_or_name="Budget.ods", task="Q4")
    assert result["status"] == "ok"
    assert result["result"] == "42"
    mock_inner.assert_called_once()
    mock_close.assert_called_once_with(opened_model, opened_for_document_research=True)
    assert not isinstance(tool, DelegateToSpecializedBase)


@patch("plugin.doc.document_research_specialized.run_inner_read_agent", return_value={"status": "error", "message": "inner failed"})
@patch("plugin.doc.document_research_specialized.close_document_research_document")
@patch("plugin.doc.document_research_specialized.open_document_for_read")
@patch("plugin.doc.document_research_specialized.resolve_path_or_name")
@patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn, *a, **k: fn())
def test_delegate_read_document_closes_after_inner_error(
    mock_main,
    mock_resolve,
    mock_open,
    mock_close,
    mock_inner,
):
    opened_model = MagicMock()
    mock_resolve.return_value = ("/tmp/Budget.ods", "file:///tmp/Budget.ods")
    mock_open.return_value = (opened_model, "calc", None, True)

    r = ToolRegistry(services={})
    r.register(DelegateReadDocument())
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.services = {"tools": r}

    result = r.get("delegate_read_document").execute_safe(ctx, path_or_name="Budget.ods", task="Q4")
    assert result["status"] == "error"
    mock_close.assert_called_once_with(opened_model, opened_for_document_research=True)


@patch("plugin.doc.document_research_specialized.run_inner_read_agent", return_value="42")
@patch("plugin.doc.document_research_specialized.close_document_research_document")
@patch("plugin.doc.document_research_specialized.open_document_for_read")
@patch("plugin.doc.document_research_specialized.resolve_path_or_name")
@patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn, *a, **k: fn())
def test_delegate_read_document_skips_close_when_reusing_open_doc(
    mock_main,
    mock_resolve,
    mock_open,
    mock_close,
    mock_inner,
):
    reused_model = MagicMock()
    mock_resolve.return_value = ("/tmp/Budget.ods", "file:///tmp/Budget.ods")
    mock_open.return_value = (reused_model, "calc", None, False)

    r = ToolRegistry(services={})
    r.register(DelegateReadDocument())
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.services = {"tools": r}

    result = r.get("delegate_read_document").execute_safe(ctx, path_or_name="Budget.ods", task="Q4")
    assert result["status"] == "ok"
    mock_close.assert_called_once_with(reused_model, opened_for_document_research=False)


def test_get_open_documents():
    from plugin.doc.document_research import get_open_documents
    from plugin.doc.document_helpers import DocumentType

    mock_ctx = MagicMock()
    mock_desktop = MagicMock()
    mock_comp = MagicMock()
    mock_comp.getController.return_value.getModel.return_value.getURL.return_value = "file:///tmp/Budget.ods"
    
    mock_desktop.getComponents.return_value.createEnumeration.return_value.hasMoreElements.side_effect = [True, False]
    mock_desktop.getComponents.return_value.createEnumeration.return_value.nextElement.return_value = mock_comp

    with patch("plugin.framework.uno_context.get_desktop", return_value=mock_desktop), \
         patch("plugin.doc.document_helpers.get_document_type", return_value=DocumentType.CALC), \
         patch("plugin.doc.document_research._system_path_from_url", return_value="/tmp/Budget.ods"):
        docs = get_open_documents(mock_ctx)
        assert len(docs) == 1
        assert docs[0]["name"] == "Budget.ods"
        assert docs[0]["doc_type"] == "calc"


@patch("plugin.chatbot.smol_agent.get_config_int", side_effect=_mock_get_config_int_for_sub_agent)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_document_research_delegation_includes_open_documents_context(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
):
    r = ToolRegistry(services={})
    r.register(ListNearbyFiles())
    r.register(GrepNearbyFiles())
    r.register(DelegateReadDocument())
    r.register(SpecializedWorkflowFinished())
    r.register(DelegateToSpecializedWriter())

    mock_get_config.return_value = {}
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    # Mock get_open_documents in specialized_base.py
    mock_docs = [
        {"name": "File1.odt", "url": "file:///tmp/File1.odt", "path": "/tmp/File1.odt", "doc_type": "writer", "is_active": True},
        {"name": "File2.ods", "url": "file:///tmp/File2.ods", "path": "/tmp/File2.ods", "doc_type": "calc", "is_active": False},
    ]

    with patch("plugin.doc.document_research.get_open_documents", return_value=mock_docs):
        ctx = MagicMock()
        ctx.doc = MagicMock()
        ctx.doc.supportsService = lambda svc: svc == "com.sun.star.text.TextDocument"
        ctx.ctx = MagicMock()
        ctx.services = {"tools": r}
        ctx.stop_checker = lambda: False

        gw = r.get("delegate_to_specialized_writer_toolset")
        result = gw.execute_safe(ctx, domain="document_research", task="Find Q4 in budget")
        assert result["status"] == "ok"
        
        # Verify instructions passed to the agent
        instructions = mock_agent_class.call_args.kwargs.get("instructions", "")
        assert "[OPEN DOCUMENTS CONTEXT]" in instructions
        assert "File1.odt [writer] (Active)" in instructions
        assert "File2.ods [calc]" in instructions
        assert "Some of these files may be completely unrelated to the task at hand" in instructions

