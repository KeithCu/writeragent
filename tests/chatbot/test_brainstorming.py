# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)

from __future__ import annotations

from unittest.mock import MagicMock, patch


from plugin.chatbot.brainstorming import (
    BrainstormResearchWeb,
    BrainstormingSessionTool,
    SaveDesignSpec,
)
from plugin.chatbot.sub_agent_tools import (
    collect_sub_agent_tools,
    normalize_html_content_array,
)
from plugin.framework.tool import ToolContext, ToolRegistry


def test_normalize_html_content_array_accepts_list_and_string():
    assert normalize_html_content_array(["<p>A</p>", "<p>B</p>"]) == ["<p>A</p>", "<p>B</p>"]
    assert normalize_html_content_array("<p>One</p>") == ["<p>One</p>"]
    assert normalize_html_content_array([]) is None
    assert normalize_html_content_array("   ") is None


def test_save_design_spec_passes_html_array_to_apply_document_content(apply_document_content_stub):
    apply_stub = apply_document_content_stub
    registry = ToolRegistry(services={})
    registry.register(apply_stub)

    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})
    tool = SaveDesignSpec()
    content = ["<h1>Design: Test</h1>", "<p>Goals</p>"]
    result = tool.execute(ctx, content=content, target="end")

    assert result["status"] == "ok"
    apply_stub.execute_safe.assert_called_once_with(ctx, content=content, target="end")


def test_save_design_spec_rejects_empty_content():
    registry = ToolRegistry(services={})
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})
    tool = SaveDesignSpec()
    result = tool.execute(ctx, content=[])
    assert result["status"] == "error"
    assert result.get("code") == "INVALID_CONTENT"


def test_collect_brainstorming_tools_merges_doc_research_reads():
    from plugin.doc.document_research_tools import ListNearbyFiles

    registry = ToolRegistry(services={})
    registry.register(BrainstormResearchWeb())
    registry.register(SaveDesignSpec())
    registry.register(ListNearbyFiles())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
    )
    names = {t.name for t in collect_sub_agent_tools(ctx, "brainstorming")}
    assert "brainstorm_research_web" in names
    assert "save_design_spec" in names
    assert "list_nearby_files" in names


def test_collect_brainstorming_tools_excludes_specialized_workflow_finished():
    from plugin.writer.specialized_base import SpecializedWorkflowFinished

    registry = ToolRegistry(services={})
    registry.register(BrainstormResearchWeb())
    registry.register(SaveDesignSpec())
    registry.register(SpecializedWorkflowFinished())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
    )
    names = {t.name for t in collect_sub_agent_tools(ctx, "brainstorming")}
    assert "brainstorm_research_web" in names
    assert "save_design_spec" in names
    assert "specialized_workflow_finished" not in names


@patch("plugin.chatbot.smol_agent.build_toolcalling_agent")
@patch("plugin.chatbot.smol_examples.get_examples_block", return_value="")
@patch("plugin.chatbot.sub_agent_tools.get_sub_agent_instructions", return_value="instr")
def test_brainstorming_session_tool_returns_ok(mock_instr, mock_examples, mock_build):
    from plugin.contrib.smolagents.memory import FinalAnswerStep

    agent = MagicMock()
    agent.run.return_value = [FinalAnswerStep(output={"status": "ok", "result": "<p>Question?</p>"})]
    mock_build.return_value = agent

    registry = ToolRegistry(services={})
    registry.register(BrainstormResearchWeb())
    registry.register(SaveDesignSpec())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
    )
    tool = BrainstormingSessionTool()
    result = tool.execute(ctx, query="hello", history_text="", topic="topic")
    assert result["status"] == "ok"
    assert "Question" in result["result"]
    tools = mock_build.call_args.args[1]
    assert any(tool_adapter.name == "reply_to_user" for tool_adapter in tools)
    assert mock_build.call_args.kwargs["examples_block"] == ""


def test_normalize_html_content_array_rejects_non_string():
    assert normalize_html_content_array([{"html": "<p>A</p>"}]) is None
    assert normalize_html_content_array(["<p>A</p>", {"bad": 1}]) is None


def test_save_design_spec_rejects_dict_item(apply_document_content_stub):
    apply_stub = apply_document_content_stub
    registry = ToolRegistry(services={})
    registry.register(apply_stub)
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})

    tool = SaveDesignSpec()
    res = tool.execute(ctx, content=[{"bad": 1}])
    assert res["status"] == "error"
    assert res["code"] == "INVALID_CONTENT"
    apply_stub.execute_safe.assert_not_called()


def test_save_design_spec_coerces_unknown_target_to_end(apply_document_content_stub):
    apply_stub = apply_document_content_stub
    registry = ToolRegistry(services={})
    registry.register(apply_stub)
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})

    tool = SaveDesignSpec()
    tool.execute(ctx, content=["<p>Hello</p>"], target="unknown_location")
    apply_stub.execute_safe.assert_called_once_with(ctx, content=["<p>Hello</p>"], target="end")


def test_save_design_spec_missing_apply_tool_errors():
    registry = ToolRegistry(services={})
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})
    tool = SaveDesignSpec()
    res = tool.execute(ctx, content=["<p>Hi</p>"])
    assert res["status"] == "error"
    assert res["code"] == "TOOL_NOT_FOUND"
