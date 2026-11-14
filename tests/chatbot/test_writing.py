# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


from plugin.chatbot.brainstorming import BrainstormResearchWeb
from plugin.chatbot.deep_research_session import DeepResearchWebTool
from plugin.chatbot.writing import (
    WritingResearchWeb,
    WritingPlanSessionTool,
    WriteDocumentSection,
)
from plugin.framework.tool import ToolContext, ToolRegistry


def test_write_document_section_passes_html_array_to_apply_document_content(apply_document_content_stub):
    apply_stub = apply_document_content_stub
    registry = ToolRegistry(services={})
    registry.register(apply_stub)

    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})
    tool = WriteDocumentSection()
    content = ["<h2>Section 1</h2>", "<p>Content goes here.</p>"]
    result = tool.execute(ctx, content=content, target="end")

    assert result["status"] == "ok"
    apply_stub.execute_safe.assert_called_once_with(ctx, content=content, target="end")


@pytest.mark.parametrize(
    "tool_cls,expected_deep",
    [
        (BrainstormResearchWeb, False),
        (WritingResearchWeb, False),
        (DeepResearchWebTool, True),
    ],
    ids=["brainstorming", "writing", "deep_research"],
)
def test_specialized_web_research_delegates_with_deep_flag(tool_cls, expected_deep):
    with patch("plugin.chatbot.web_research.WebResearchTool") as mock_cls:
        instance = mock_cls.return_value
        instance.execute.return_value = {"status": "ok", "result": "facts"}
        ctx = MagicMock()
        out = tool_cls().execute(ctx, query="topic")
        instance.execute.assert_called_once_with(ctx, query="topic", deep=expected_deep)
        assert out["result"] == "facts"


@patch("plugin.chatbot.smol_agent.build_toolcalling_agent")
@patch("plugin.chatbot.smol_examples.get_examples_block", return_value="")
@patch("plugin.chatbot.sub_agent_tools.get_sub_agent_instructions", return_value="instr")
def test_writing_plan_session_tool_returns_ok(mock_instr, mock_examples, mock_build):
    from plugin.contrib.smolagents.memory import FinalAnswerStep

    agent = MagicMock()
    agent.run.return_value = [FinalAnswerStep(output={"status": "ok", "result": "<p>Question?</p>"})]
    mock_build.return_value = agent

    registry = ToolRegistry(services={})
    registry.register(WritingResearchWeb())
    registry.register(WriteDocumentSection())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
    )
    tool = WritingPlanSessionTool()
    result = tool.execute(ctx, query="hello", history_text="", topic="topic")
    assert result["status"] == "ok"
    assert "Question" in result["result"]
    tools = mock_build.call_args.args[1]
    assert any(tool_adapter.name == "reply_to_user" for tool_adapter in tools)


def test_write_document_section_rejects_dict_item(apply_document_content_stub):
    apply_stub = apply_document_content_stub
    registry = ToolRegistry(services={})
    registry.register(apply_stub)
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})

    tool = WriteDocumentSection()
    res = tool.execute(ctx, content=[{"bad": 1}])
    assert res["status"] == "error"
    assert res["code"] == "INVALID_CONTENT"
    apply_stub.execute_safe.assert_not_called()


def test_write_document_section_rejects_empty_content():
    registry = ToolRegistry(services={})
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})
    tool = WriteDocumentSection()
    res = tool.execute(ctx, content=[])
    assert res["status"] == "error"
    assert res["code"] == "INVALID_CONTENT"


def test_write_document_section_coerces_unknown_target_to_end(apply_document_content_stub):
    apply_stub = apply_document_content_stub
    registry = ToolRegistry(services={})
    registry.register(apply_stub)
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})

    tool = WriteDocumentSection()
    tool.execute(ctx, content=["<p>Hello</p>"], target="unknown_location")
    apply_stub.execute_safe.assert_called_once_with(ctx, content=["<p>Hello</p>"], target="end")


def test_write_document_section_missing_apply_tool_errors():
    registry = ToolRegistry(services={})
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})
    tool = WriteDocumentSection()
    res = tool.execute(ctx, content=["<p>Hi</p>"])
    assert res["status"] == "error"
    assert res["code"] == "TOOL_NOT_FOUND"
