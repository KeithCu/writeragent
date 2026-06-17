# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.brainstorming import (
    BrainstormResearchWeb,
    BrainstormingFinishedTool,
    BrainstormingSessionTool,
    SaveDesignSpec,
    _normalize_html_content_array,
    collect_brainstorming_tools,
)
from plugin.chatbot.smol_examples import get_examples_block
from plugin.framework.constants import get_brainstorming_sub_agent_instructions
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry
from plugin.writer.specialized_base import DelegateToSpecializedWriter


def _brainstorming_domains(gateway):
    return gateway.parameters["properties"]["domain"]["enum"]


def test_brainstorming_not_in_writer_delegate_enum():
    gw = DelegateToSpecializedWriter()
    assert "brainstorming" not in _brainstorming_domains(gw)


def test_writing_plan_not_in_writer_delegate_enum():
    gw = DelegateToSpecializedWriter()
    assert "writing_plan" not in _brainstorming_domains(gw)


def test_brainstorming_examples_use_html_only():
    block = get_examples_block("brainstorming")
    assert "<p>" in block
    assert "<h1>" in block
    assert "**" not in block
    assert "# Design" not in block


def test_brainstorming_examples_show_approaches_and_self_review():
    block = get_examples_block("brainstorming")
    assert "Recommended" in block
    assert "<h2>Architecture</h2>" in block
    assert "Self-review" in block
    assert "<h2>Testing</h2>" in block


def test_brainstorming_instructions_include_superpowers_self_review():
    text = get_brainstorming_sub_agent_instructions()
    assert "Placeholder scan" in text
    assert "Spec self-review" in text
    assert "YAGNI" in text
    assert "too simple to design" in text


def test_normalize_html_content_array_accepts_list_and_string():
    assert _normalize_html_content_array(["<p>A</p>", "<p>B</p>"]) == ["<p>A</p>", "<p>B</p>"]
    assert _normalize_html_content_array("<p>One</p>") == ["<p>One</p>"]
    assert _normalize_html_content_array([]) is None
    assert _normalize_html_content_array("   ") is None


class _ApplyDocumentContentStub(ToolBase):
    name = "apply_document_content"
    description = "stub"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self):
        super().__init__()
        self.execute_safe = MagicMock(return_value={"status": "ok", "message": "applied"})

    def execute(self, ctx, **kwargs):
        return self.execute_safe(ctx, **kwargs)


def test_save_design_spec_passes_html_array_to_apply_document_content():
    apply_stub = _ApplyDocumentContentStub()
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


def test_brainstorm_research_web_delegates_to_web_research():
    with patch("plugin.chatbot.web_research.WebResearchTool") as mock_cls:
        instance = mock_cls.return_value
        instance.execute.return_value = {"status": "ok", "result": "facts"}
        ctx = MagicMock()
        tool = BrainstormResearchWeb()
        out = tool.execute(ctx, query="topic")
        instance.execute.assert_called_once_with(ctx, query="topic")
        assert out["result"] == "facts"


def test_brainstorming_finished_returns_finished_status():
    tool = BrainstormingFinishedTool()
    result = tool.execute(MagicMock(), message="<p>Done.</p>", spec_saved=True)
    assert result["status"] == "finished"
    assert result["result"] == "<p>Done.</p>"
    assert result["spec_saved"] is True


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
    names = {t.name for t in collect_brainstorming_tools(ctx)}
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
    names = {t.name for t in collect_brainstorming_tools(ctx)}
    assert "brainstorm_research_web" in names
    assert "save_design_spec" in names
    assert "specialized_workflow_finished" not in names


@patch("plugin.chatbot.smol_agent.build_toolcalling_agent")
@patch("plugin.chatbot.smol_examples.get_examples_block", return_value="")
@patch("plugin.framework.constants.get_brainstorming_sub_agent_instructions", return_value="instr")
def test_brainstorming_session_tool_returns_ok(mock_instr, mock_examples, mock_build):
    from plugin.contrib.smolagents.memory import FinalAnswerStep

    agent = MagicMock()
    agent.run.return_value = [FinalAnswerStep(output="<p>Question?</p>")]
    mock_build.return_value = agent

    registry = ToolRegistry(services={})
    registry.register(BrainstormResearchWeb())
    registry.register(SaveDesignSpec())

    ctx = ToolContext(
        doc=MagicMock(),
        ctx=MagicMock(),
        doc_type="writer",
        services={"tools": registry},
        stop_checker=lambda: False,
    )
    tool = BrainstormingSessionTool()
    result = tool.execute(ctx, query="hello", history_text="", topic="topic")
    assert result["status"] == "ok"
    assert "Question" in result["result"]
