# WriterAgent - tests for Deep Research sidebar session

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.chatbot.deep_research_session import (
    DeepResearchSessionTool,
    DeepResearchWebTool,
    collect_deep_research_tools,
)
from plugin.chatbot.smol_examples import get_examples_block
from plugin.framework.constants import get_deep_research_sub_agent_instructions
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry
from plugin.writer.specialized_base import DelegateToSpecializedWriter


def test_deep_research_not_in_writer_delegate_enum():
    gw = DelegateToSpecializedWriter()
    domains = gw.parameters["properties"]["domain"]["enum"]
    assert "deep_research" not in domains


def test_deep_research_instructions_include_apply_document_content():
    text = get_deep_research_sub_agent_instructions()
    assert "apply_document_content" in text
    assert "deep_research_web" in text


def test_deep_research_examples_include_apply_document_content():
    block = get_examples_block("deep_research")
    assert "apply_document_content" in block
    assert "deep_research_web" in block
    assert "<h1>" in block


def test_deep_research_web_delegates_with_deep_true():
    with patch("plugin.chatbot.web_research.WebResearchTool") as mock_cls:
        instance = mock_cls.return_value
        instance.execute.return_value = {"status": "ok", "result": "deep report"}
        ctx = MagicMock()
        tool = DeepResearchWebTool()
        out = tool.execute(ctx, query="topic")
        instance.execute.assert_called_once_with(ctx, query="topic", deep=True)
        assert out["result"] == "deep report"


class _ApplyDocumentContentStub(ToolBase):
    name = "apply_document_content"
    description = "stub"
    tier = "core"
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class _GetDocumentContentStub(ToolBase):
    name = "get_document_content"
    description = "stub"
    tier = "core"
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "content": ""}


def test_collect_deep_research_tools_includes_apply_on_writer():
    registry = ToolRegistry(services={})
    registry.register(DeepResearchWebTool())
    registry.register(_ApplyDocumentContentStub())
    registry.register(_GetDocumentContentStub())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
    )
    names = {t.name for t in collect_deep_research_tools(ctx)}
    assert "deep_research_web" in names
    assert "apply_document_content" in names
    assert "get_document_content" in names


@patch("plugin.chatbot.smol_agent.build_toolcalling_agent")
@patch("plugin.chatbot.smol_examples.get_examples_block", return_value="")
@patch("plugin.framework.constants.get_deep_research_sub_agent_instructions", return_value="instr")
def test_deep_research_session_tool_returns_ok(mock_instr, mock_examples, mock_build):
    from plugin.contrib.smolagents.memory import FinalAnswerStep

    agent = MagicMock()
    agent.run.return_value = [FinalAnswerStep(output="<p>Research added to document.</p>")]
    mock_build.return_value = agent

    registry = ToolRegistry(services={})
    registry.register(DeepResearchWebTool())
    registry.register(_ApplyDocumentContentStub())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
    )
    tool = DeepResearchSessionTool()
    result = tool.execute(ctx, query="Research topic")
    assert result["status"] == "ok"
    assert "document" in result["result"]
