# WriterAgent - tests for deep web research (sidebar deep=True path and orchestrator)

from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.web_research_deep import (
    parse_follow_up_questions_response,
    parse_research_results_response,
    parse_search_queries_response,
    run_deep_research,
    trim_context_to_word_limit,
)
class TestDeepResearchParsers:
    def test_parse_search_queries_json_array(self):
        raw = '[{"query": "foo bar", "researchGoal": "learn foo"}]'
        out = parse_search_queries_response(raw, 3)
        assert out == [{"query": "foo bar", "researchGoal": "learn foo"}]

    def test_parse_search_queries_line_fallback(self):
        raw = "Query: climate policy\nResearch Goal: survey regulations"
        out = parse_search_queries_response(raw, 3)
        assert out == [{"query": "climate policy", "researchGoal": "survey regulations"}]

    def test_parse_follow_up_questions_json(self):
        raw = '{"questions": ["What changed in 2024?", "Who leads the field?"]}'
        out = parse_follow_up_questions_response(raw, 3)
        assert len(out) == 2
        assert "2024" in out[0]

    def test_parse_research_results_json(self):
        raw = (
            '{"learnings": [{"insight": "Alpha found", "sourceUrl": "https://example.com"}], '
            '"followUpQuestions": ["What about beta?"]}'
        )
        out = parse_research_results_response(raw, 3)
        assert out["learnings"] == ["Alpha found"]
        assert out["citations"]["Alpha found"] == "https://example.com"
        assert out["followUpQuestions"] == ["What about beta?"]

    def test_trim_context_to_word_limit(self):
        chunks = ["one two three", "four five"]
        trimmed = trim_context_to_word_limit(chunks, max_words=4)
        # Keeps most recent chunks first when trimming from the end of the list.
        assert len(trimmed) == 1
        assert trimmed[0] == "four five"


class TestRunDeepResearch:
    def _llm_router(self, messages):
        combined = "\n".join(m["content"] for m in messages)
        if "search queries" in combined and "JSON array" in combined:
            return '[{"query": "sub one", "researchGoal": "goal one"}]'
        if "follow-up questions" in combined.lower() or ('"questions"' in combined and "JSON object" in combined):
            return '{"questions": ["Aspect A?"]}'
        if "extract key learnings" in combined:
            return '{"learnings": [{"insight": "Finding X", "sourceUrl": "https://a.test"}], "followUpQuestions": []}'
        if "Collected evidence" in combined or "plain-text research report" in combined:
            return "Final synthesized report."
        return "{}"

    def test_run_deep_research_happy_path(self):
        calls: list[str] = []

        def run_web_agent(sub_query, _history):
            calls.append(sub_query)
            return "Sub-agent context for " + sub_query

        result = run_deep_research(
            "main topic",
            None,
            llm_chat=lambda msgs, _max: self._llm_router(msgs),
            run_web_agent=run_web_agent,
            stop_checker=None,
            status_callback=None,
            breadth=1,
            depth=1,
            plain_text_format="Use plain text.",
            initial_search_snippet="preview hit",
        )
        assert result == "Final synthesized report."
        assert len(calls) == 1
        assert calls[0] == "sub one"

    def test_run_deep_research_stop_checker(self):
        def run_web_agent(_sub_query, _history):
            return "should not run"

        result = run_deep_research(
            "topic",
            None,
            llm_chat=lambda msgs, _max: self._llm_router(msgs),
            run_web_agent=run_web_agent,
            stop_checker=lambda: True,
            status_callback=None,
            breadth=1,
            depth=1,
            plain_text_format="plain",
        )
        assert isinstance(result, dict)
        assert result.get("status") == "error"
        assert result.get("code") == "USER_STOPPED"


class TestWebResearchExecuteDeepKwarg:
    @patch("plugin.chatbot.web_research._run_web_agent")
    def test_default_calls_run_web_agent_once(self, mock_run):
        from plugin.chatbot.web_research import WebResearchTool

        mock_run.return_value = "shallow report"
        tool = WebResearchTool()
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.doc = None
        ctx.status_callback = None
        ctx.append_thinking_callback = None
        ctx.approval_callback = None
        ctx.chat_append_callback = None
        ctx.stop_checker = None
        ctx.send_cancellation = None

        with (
            patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en", "english")),
            patch("plugin.framework.config.get_config_bool_safe", return_value=False),
            patch("plugin.framework.config.user_config_dir", return_value=None),
            patch("plugin.framework.config.get_config_int", side_effect=lambda key: 15 if "max_tool" in key else 50),
            patch("plugin.framework.config.get_api_config", return_value={}),
            patch("plugin.framework.client.llm_client.LlmClient"),
            patch("plugin.chatbot.smol_agent.WriterAgentSmolModel"),
            patch("plugin.framework.config.get_config", return_value="off"),
        ):
            out = tool.execute(ctx, query="test query")

        assert out["status"] == "ok"
        assert out["result"] == "shallow report"
        mock_run.assert_called_once()

    @patch("plugin.chatbot.web_research._run_web_agent")
    @patch("plugin.chatbot.web_research._run_deep_web_research")
    def test_deep_kwarg_calls_run_deep_web_research(self, mock_deep, mock_run):
        from plugin.chatbot.web_research import WebResearchTool

        mock_deep.return_value = "deep report"
        tool = WebResearchTool()
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.doc = None
        ctx.status_callback = None
        ctx.append_thinking_callback = None
        ctx.approval_callback = None
        ctx.chat_append_callback = None
        ctx.stop_checker = None
        ctx.send_cancellation = None

        with (
            patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en", "english")),
            patch("plugin.framework.config.get_config_bool_safe", return_value=False),
            patch("plugin.framework.config.user_config_dir", return_value=None),
            patch("plugin.framework.config.get_config_int", side_effect=lambda key: 4 if "breadth" in key else (2 if "depth" in key else 15)),
            patch("plugin.framework.config.get_api_config", return_value={}),
            patch("plugin.framework.client.llm_client.LlmClient"),
            patch("plugin.chatbot.smol_agent.WriterAgentSmolModel"),
            patch("plugin.framework.config.get_config", return_value="off"),
        ):
            out = tool.execute(ctx, query="deep topic", deep=True)

        assert out["status"] == "ok"
        assert out["result"] == "deep report"
        mock_deep.assert_called_once()
        mock_run.assert_not_called()
