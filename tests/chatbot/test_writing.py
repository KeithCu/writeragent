# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)

from __future__ import annotations

from unittest.mock import MagicMock, patch


from plugin.chatbot.writing import (
    WRITING_SUB_AGENT_INSTRUCTIONS,
    WritingResearchWeb,
    WritingPlanSessionTool,
    WriteDocumentSection,
)
from plugin.chatbot.smol_examples import get_examples_block
from plugin.chatbot.sub_agent_tools import (
    collect_sub_agent_tools,
    get_sub_agent_instructions,
)
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry


def test_writing_plan_examples_use_html_only():
    block = get_examples_block("writing_plan")
    assert "<p>" in block
    assert "<h2>" in block
    assert "**" not in block
    assert "# " not in block


def test_writing_instructions_content():
    text = get_sub_agent_instructions(WRITING_SUB_AGENT_INSTRUCTIONS)
    assert "WRITING PLAN MODE" in text
    assert "write_document_section" in text
    assert "writing_research_web" in text


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


def test_writing_research_web_delegates_to_web_research():
    with patch("plugin.chatbot.web_research.WebResearchTool") as mock_cls:
        instance = mock_cls.return_value
        instance.execute.return_value = {"status": "ok", "result": "facts"}
        ctx = MagicMock()
        tool = WritingResearchWeb()
        out = tool.execute(ctx, query="topic")
        instance.execute.assert_called_once_with(ctx, query="topic")
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
@patch("plugin.chatbot.writing.get_sub_agent_instructions", return_value="instr")
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
        stop_checker=lambda: False,
    )
    tool = WritingPlanSessionTool()
    result = tool.execute(ctx, query="hello", history_text="", topic="topic")
    assert result["status"] == "ok"
    assert "Question" in result["result"]
    tools = mock_build.call_args.args[1]
    assert any(tool_adapter.name == "reply_to_user" for tool_adapter in tools)
    assert mock_build.call_args.kwargs["examples_block"] == ""


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


def test_write_document_section_is_async():
    tool = WriteDocumentSection()
    assert tool.is_async() is True


def test_writing_plan_session_tool_supports_writer_not_calc():
    from plugin.framework.tool import tool_supports_document

    tool = WritingPlanSessionTool()
    assert tool_supports_document(tool, doc_type="writer", uno_services_supported=None) is True
    assert tool_supports_document(tool, doc_type="calc", uno_services_supported=None) is False


def test_collect_writing_tools_does_not_include_apply_document_content():
    registry = ToolRegistry(services={})
    registry.register(WritingResearchWeb())
    registry.register(WriteDocumentSection())
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})
    names = {t.name for t in collect_sub_agent_tools(ctx, "writing_plan")}
    assert "apply_document_content" not in names
