# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for sub_agent_tools (collectors, session tools, runner error handling)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.brainstorming import BrainstormingSessionTool
from plugin.chatbot.deep_research_session import DeepResearchSessionTool
from plugin.chatbot.librarian import LibrarianOnboardingTool
from plugin.chatbot.ppt_master import PptMasterSessionTool
from plugin.chatbot.sub_agent_tools import (
    SubAgentSessionToolBase,
    collect_sub_agent_tools,
    get_sub_agent_instructions,
    run_subagent_tool,
)
from plugin.chatbot.writing import WritingPlanSessionTool
from plugin.framework.errors import (
    DocumentDisposedError,
    WriterAgentException,
)
from plugin.framework.tool import ToolBase, ToolContext


class TestRunSubagentTool:
    def test_run_subagent_tool_success(self):
        ctx = MagicMock()
        runner = MagicMock(return_value={"status": "ok", "result": "done"})

        result = run_subagent_tool("Writing plan", runner, ctx, query="Draft essay", history_text="hi", topic="AI")

        assert result == {"status": "ok", "result": "done"}
        runner.assert_called_once_with(ctx, query="Draft essay", history_text="hi", topic="AI")

    def test_run_subagent_tool_catches_exception_and_formats_error(self, caplog):
        ctx = MagicMock()

        def failing_runner(c, **kwargs):
            raise RuntimeError("API timeout")

        with caplog.at_level("ERROR", logger="writeragent.sub_agent_tools"):
            result = run_subagent_tool("Brainstorming", failing_runner, ctx, query="Brainstorm ideas")

        assert result.get("status") == "error"
        message = result.get("message", "")
        assert "Brainstorming failed: API timeout" in message
        assert "Traceback" not in message
        assert "RuntimeError" not in message
        assert result.get("details") == {"query": "Brainstorm ideas"}
        assert any(r.exc_info is not None and "Brainstorming execution failed" in r.message for r in caplog.records)

    def test_run_subagent_tool_with_no_query_in_kwargs(self):
        ctx = MagicMock()

        def failing_runner(c, **kwargs):
            raise ValueError("bad input")

        result = run_subagent_tool("Deep research", failing_runner, ctx)

        assert result.get("status") == "error"
        assert "Deep research failed: bad input" in result.get("message", "")
        assert result.get("details") == {"query": None}

    def test_user_stopped_code_preserved_and_details_include_query(self):
        ctx = MagicMock()

        def stopped_runner(c, **kwargs):
            raise WriterAgentException("Operation aborted by user", code="USER_STOPPED")

        result = run_subagent_tool("Writing plan", stopped_runner, ctx, query="Draft essay")
        assert result.get("status") == "error"
        assert result.get("code") == "USER_STOPPED"
        assert result.get("details") == {"query": "Draft essay"}
        assert "Operation aborted by user" in result.get("message", "")

    def test_disposal_reraised_off_main_thread(self):
        ctx = MagicMock()

        def disposed_runner(c, **kwargs):
            raise DocumentDisposedError("Document was disposed")

        with pytest.raises(DocumentDisposedError):
            run_subagent_tool("Writing plan", disposed_runner, ctx, query="Draft essay")

    def test_empty_str_with_message_attr_still_produces_message(self):
        ctx = MagicMock()

        class UnoStyleError(Exception):
            def __init__(self, msg: str):
                self.Message = msg

            def __str__(self):
                return ""

        def uno_runner(c, **kwargs):
            raise UnoStyleError("com.sun.star.uno.RuntimeException: document failed")

        result = run_subagent_tool("Writing plan", uno_runner, ctx, query="Draft")
        assert result.get("status") == "error"
        assert "document failed" in result.get("message", "")


class TestSubagentCollector:
    def test_primary_domain_wins_over_same_named_doc_research_tool(self):
        class _PrimaryTool(ToolBase):
            name = "shared_tool"
            tag = "primary"

            def execute(self, ctx, **kwargs):
                return {}

        class _DocResTool(ToolBase):
            name = "shared_tool"
            tag = "doc_res"

            def execute(self, ctx, **kwargs):
                return {}

        def mock_get_tools(*args, active_domain=None, **kwargs):
            if active_domain == "writing_plan":
                return [_PrimaryTool()]
            if active_domain == "document_research":
                return [_DocResTool()]
            return []

        registry = MagicMock()
        registry.get_tools.side_effect = mock_get_tools
        ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})

        with patch("plugin.chatbot.sub_agent_tools.DOC_RESEARCH_DISCOVERY_TOOL_NAMES", {"shared_tool"}):
            tools = collect_sub_agent_tools(ctx, "writing_plan")
            assert len(tools) == 1
            assert getattr(tools[0], "tag", "") == "primary"

    def test_discovery_tools_once(self):
        class _DiscTool(ToolBase):
            name = "list_nearby_files"

            def execute(self, ctx, **kwargs):
                return {}

        def mock_get_tools(*args, active_domain=None, **kwargs):
            if active_domain == "writing_plan":
                return []
            if active_domain == "document_research":
                return [_DiscTool(), _DiscTool()]
            return []

        registry = MagicMock()
        registry.get_tools.side_effect = mock_get_tools
        ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})

        tools = collect_sub_agent_tools(ctx, "writing_plan")
        assert len([t for t in tools if t.name == "list_nearby_files"]) == 1

    def test_search_nearby_files_follows_folder_search_enabled(self):
        class _SearchTool(ToolBase):
            name = "search_nearby_files"

            def execute(self, ctx, **kwargs):
                return {}

        class _GrepTool(ToolBase):
            name = "grep_nearby_files"

            def execute(self, ctx, **kwargs):
                return {}

        def mock_get_tools(*args, active_domain=None, **kwargs):
            if active_domain == "writing_plan":
                return []
            if active_domain == "document_research":
                return [_SearchTool(), _GrepTool()]
            return []

        registry = MagicMock()
        registry.get_tools.side_effect = mock_get_tools
        ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})

        with patch("plugin.framework.constants.folder_search_enabled", return_value=False):
            tools_disabled = collect_sub_agent_tools(ctx, "writing_plan")
            names_disabled = {t.name for t in tools_disabled}
            assert "grep_nearby_files" in names_disabled
            assert "search_nearby_files" not in names_disabled

        with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
            tools_enabled = collect_sub_agent_tools(ctx, "writing_plan")
            names_enabled = {t.name for t in tools_enabled}
            assert "search_nearby_files" in names_enabled
            assert "grep_nearby_files" not in names_enabled

    def test_deep_collector_is_shared_collector_with_discovery_off(self):
        class _DeepTool(ToolBase):
            name = "deep_research_web"

            def execute(self, ctx, **kwargs):
                return {}

        class _DiscTool(ToolBase):
            name = "list_nearby_files"

            def execute(self, ctx, **kwargs):
                return {}

        def mock_get_tools(*args, active_domain=None, **kwargs):
            if active_domain == "deep_research":
                return [_DeepTool()]
            if active_domain == "document_research":
                return [_DiscTool()]
            return []

        registry = MagicMock()
        registry.get_tools.side_effect = mock_get_tools
        ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services={"tools": registry})

        tools = collect_sub_agent_tools(ctx, "deep_research", include_document_research=False)
        names = {t.name for t in tools}
        assert "deep_research_web" in names
        assert "list_nearby_files" not in names


class TestSessionToolMetadata:
    @pytest.mark.parametrize(
        "tool_cls",
        [
            BrainstormingSessionTool,
            WritingPlanSessionTool,
            DeepResearchSessionTool,
            PptMasterSessionTool,
            LibrarianOnboardingTool,
        ],
    )
    def test_session_tool_metadata_and_execute(self, tool_cls):
        tool = tool_cls()
        assert tool.is_async() is True
        assert tool.is_mutation is False
        assert isinstance(tool, SubAgentSessionToolBase)

        ctx = MagicMock()
        with patch("plugin.chatbot.sub_agent_tools.run_subagent_tool") as mock_run:
            mock_run.return_value = {"status": "ok"}
            res = tool.execute(ctx, query="test query")
            assert res == {"status": "ok"}
            mock_run.assert_called_once_with(tool.agent_label, tool.runner, ctx, query="test query")


class TestPromptToolMatching:
    def test_sub_agent_instructions_prompt_tool_matching(self):
        # Brainstorming: save_design_spec only -> content rules present, target search rules absent
        b_instr = get_sub_agent_instructions("BASE", tool_names=["save_design_spec"])
        assert "`content` is a JSON array of HTML strings" in b_instr
        assert "target='search'" not in b_instr

        # Writing plan: write_document_section only -> content rules present, target search rules absent
        w_instr = get_sub_agent_instructions("BASE", tool_names=["write_document_section"])
        assert "`content` is a JSON array of HTML strings" in w_instr
        assert "target='search'" not in w_instr

        # Deep research with apply_document_content -> both present
        d_instr = get_sub_agent_instructions("BASE", tool_names=["apply_document_content"])
        assert "`content` is a JSON array of HTML strings" in d_instr
        assert "APPLY_DOCUMENT_CONTENT AND HTML (CRITICAL):" in d_instr
        assert "target='search'" in d_instr

        # No HTML tools -> neither content nor target rules
        plain_instr = get_sub_agent_instructions("BASE", tool_names=["other_tool"])
        assert "`content` is a JSON array of HTML strings" not in plain_instr
        assert "target='search'" not in plain_instr
