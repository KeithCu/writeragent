# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


from plugin.chatbot.writing import (
    WritingResearchWeb,
    WritingPlanSessionTool,
    WriteDocumentSection,
)
from plugin.chatbot.sub_agent_tools import collect_sub_agent_tools
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry


class _ApplyDocumentContentStub(ToolBase):
    name = "apply_document_content"
    description = "stub"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self):
        super().__init__()
        self.execute_safe = MagicMock(return_value={"status": "ok", "message": "applied"})

    def execute(self, ctx, **kwargs):
        return self.execute_safe(ctx, **kwargs)


def test_write_document_section_passes_html_array_to_apply_document_content():
    apply_stub = _ApplyDocumentContentStub()
    registry = ToolRegistry(services={})
    registry.register(apply_stub)

    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})
    tool = WriteDocumentSection()
    content = ["<h2>Section 1</h2>", "<p>Content goes here.</p>"]
    result = tool.execute(ctx, content=content, target="end")

    assert result["status"] == "ok"
    apply_stub.execute_safe.assert_called_once_with(ctx, content=content, target="end")


@pytest.mark.parametrize(
    "tool_cls_path,expected_deep",
    [
        ("plugin.chatbot.brainstorming.BrainstormResearchWeb", False),
        ("plugin.chatbot.writing.WritingResearchWeb", False),
        ("plugin.chatbot.deep_research_session.DeepResearchWebTool", True),
    ],
)
def test_specialized_web_research_delegates_with_deep_flag(tool_cls_path, expected_deep):
    import importlib

    mod_name, cls_name = tool_cls_path.rsplit(".", 1)
    tool_cls = getattr(importlib.import_module(mod_name), cls_name)
    with patch("plugin.chatbot.web_research.WebResearchTool") as mock_cls:
        instance = mock_cls.return_value
        instance.execute.return_value = {"status": "ok", "result": "facts"}
        ctx = MagicMock()
        out = tool_cls().execute(ctx, query="topic")
        instance.execute.assert_called_once_with(ctx, query="topic", deep=expected_deep)
        assert out["result"] == "facts"


def test_collect_writing_tools_merges_doc_research_reads():
    from plugin.doc.document_research_tools import ListNearbyFiles

    registry = ToolRegistry(services={})
    registry.register(WritingResearchWeb())
    registry.register(WriteDocumentSection())
    registry.register(ListNearbyFiles())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
    )
    names = {t.name for t in collect_sub_agent_tools(ctx, "writing_plan")}
    assert "writing_research_web" in names
    assert "write_document_section" in names
    assert "list_nearby_files" in names


def test_collect_writing_tools_excludes_specialized_workflow_finished():
    from plugin.writer.specialized_base import SpecializedWorkflowFinished

    registry = ToolRegistry(services={})
    registry.register(WritingResearchWeb())
    registry.register(WriteDocumentSection())
    registry.register(SpecializedWorkflowFinished())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
    )
    names = {t.name for t in collect_sub_agent_tools(ctx, "writing_plan")}
    assert "writing_research_web" in names
    assert "write_document_section" in names
    assert "specialized_workflow_finished" not in names


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


def test_write_document_section_rejects_dict_item():
    apply_stub = _ApplyDocumentContentStub()
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


def test_write_document_section_coerces_unknown_target_to_end():
    apply_stub = _ApplyDocumentContentStub()
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
