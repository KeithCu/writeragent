# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Tests for workspace delegation and read-only enforcement."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.specialized import DelegateToSpecializedCalc
from plugin.contrib.smolagents.memory import FinalAnswerStep
from plugin.doc.nearby_specialized import DelegateReadDocument, run_inner_read_agent
from plugin.doc.specialized_base import DelegateToSpecializedBase
from plugin.draw.specialized import DelegateToSpecializedDraw
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry
from tests.chatbot.test_tool_loop import _mock_get_config_int_for_sub_agent
from plugin.writer.specialized_base import DelegateToSpecializedWriter, SpecializedWorkflowFinished
from plugin.doc.nearby_tools import ListNearbyFiles


class _MutatingTool(ToolBase):
    name = "apply_fake"
    description = "test mutator"
    is_mutation = True
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


def _workspace_domains(gateway):
    return gateway.parameters["properties"]["domain"]["enum"]


def test_workspace_in_writer_delegate_enum():
    gw = DelegateToSpecializedWriter()
    assert "workspace" in _workspace_domains(gw)


def test_workspace_in_calc_delegate_enum():
    gw = DelegateToSpecializedCalc()
    assert "workspace" in _workspace_domains(gw)


def test_workspace_in_draw_delegate_enum():
    gw = DelegateToSpecializedDraw()
    assert "workspace" in _workspace_domains(gw)


def test_workspace_tools_registered_with_cross_cutting():
    r = ToolRegistry(services={})
    r.register(ListNearbyFiles())
    r.register(DelegateReadDocument())
    r.register(SpecializedWorkflowFinished())
    tools = r.get_tools(doc=MagicMock(), active_domain="workspace", exclude_tiers=())
    names = {t.name for t in tools}
    assert "list_nearby_files" in names
    assert "delegate_read_document" in names
    assert "specialized_workflow_finished" in names


@patch("plugin.doc.specialized_base.USE_SUB_AGENT", False)
def test_workspace_requires_sub_agent_when_disabled():
    r = ToolRegistry(services={})
    r.register(DelegateToSpecializedWriter())
    gw = r.get("delegate_to_specialized_writer_toolset")
    ctx = MagicMock()
    ctx.services = {"tools": r}
    result = gw.execute_safe(ctx, domain="workspace", task="read budget")
    assert result["status"] == "error"
    assert "WORKSPACE_REQUIRES_SUB_AGENT" in result.get("code", "") or "sub-agent" in result.get("message", "").lower()


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
def test_workspace_outer_delegation_gets_workspace_tools(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
):
    r = ToolRegistry(services={})
    r.register(ListNearbyFiles())
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
    result = gw.execute_safe(ctx, domain="workspace", task="Find Q4 in budget")
    assert result["status"] == "ok"
    smol_tools = mock_agent_class.call_args.kwargs.get("tools", [])
    names = {t.name for t in smol_tools}
    assert "list_nearby_files" in names
    assert "delegate_read_document" in names


@patch("plugin.doc.nearby_specialized.build_toolcalling_agent")
@patch("plugin.doc.nearby_specialized.SmolAgentExecutor")
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


@patch("plugin.doc.nearby_specialized.run_inner_read_agent", return_value={"status": "ok", "result": "42"})
@patch("plugin.doc.nearby_specialized.close_workspace_document")
@patch("plugin.doc.nearby_specialized.open_document_for_read")
@patch("plugin.doc.nearby_specialized.resolve_path_or_name")
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
    mock_close.assert_called_once_with(opened_model, opened_for_workspace=True)
    assert not isinstance(tool, DelegateToSpecializedBase)


@patch("plugin.doc.nearby_specialized.run_inner_read_agent", return_value={"status": "error", "message": "inner failed"})
@patch("plugin.doc.nearby_specialized.close_workspace_document")
@patch("plugin.doc.nearby_specialized.open_document_for_read")
@patch("plugin.doc.nearby_specialized.resolve_path_or_name")
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
    mock_close.assert_called_once_with(opened_model, opened_for_workspace=True)


@patch("plugin.doc.nearby_specialized.run_inner_read_agent", return_value="42")
@patch("plugin.doc.nearby_specialized.close_workspace_document")
@patch("plugin.doc.nearby_specialized.open_document_for_read")
@patch("plugin.doc.nearby_specialized.resolve_path_or_name")
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
    mock_close.assert_called_once_with(reused_model, opened_for_workspace=False)
