# WriterAgent - tests for deep web research (adaptive loop + orchestrator)

from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.web_research_deep import (
    ResearchProgress,
    assess_research_coverage,
    parse_assessment_response,
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

    def test_parse_assessment_response(self):
        raw = '{"score": 8, "knowledge_gaps": ["gap1"], "suggested_queries": ["q1"], "stop": false, "reasoning": "ok"}'
        out = parse_assessment_response(raw)
        assert out["score"] == 8.0
        assert out["knowledge_gaps"] == ["gap1"]
        assert out["suggested_queries"] == ["q1"]
        assert out["stop"] is False

    def test_trim_context_to_word_limit(self):
        chunks = ["one two three", "four five"]
        trimmed = trim_context_to_word_limit(chunks, max_words=4)
        assert len(trimmed) == 1
        assert trimmed[0] == "four five"

    def test_research_progress_status_text(self):
        p = ResearchProgress(current_round=2, max_rounds=3, completed_queries=5, max_sub_queries=14, current_query="climate policy")
        assert "round 2/3" in p.status_text()
        assert "5/14" in p.status_text()


class TestRunDeepResearch:
    def _llm_router(self, messages):
        combined = "\n".join(m["content"] for m in messages)
        if "search queries" in combined and "JSON array" in combined:
            return '[{"query": "sub one", "researchGoal": "goal one"}]'
        if "follow-up questions" in combined.lower() or ('"questions"' in combined and "JSON object" in combined):
            return '{"questions": ["Aspect A?"]}'
        if "extract key learnings" in combined:
            return '{"learnings": [{"insight": "Finding X", "sourceUrl": "https://a.test"}], "followUpQuestions": []}'
        if "evaluate whether web research" in combined or "Quality threshold" in combined:
            return '{"score": 9, "knowledge_gaps": [], "suggested_queries": [], "stop": true, "reasoning": "sufficient"}'
        if "plain-text research report" in combined or "Collected evidence" in combined:
            return "Final synthesized report."
        return "{}"

    def test_run_deep_research_happy_path(self):
        calls: list[tuple[str, str]] = []

        def run_web_agent(sub_query, research_goal, _history):
            calls.append((sub_query, research_goal))
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
            max_sub_queries=5,
        )
        assert result == "Final synthesized report."
        assert len(calls) == 1
        assert calls[0][0] == "sub one"
        assert calls[0][1] == "goal one"

    def test_run_deep_research_stop_checker(self):
        def run_web_agent(_sub_query, _goal, _history):
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

    def test_max_sub_queries_caps_parallel_runs(self):
        call_count = 0

        def run_web_agent(_sub_query, _goal, _history):
            nonlocal call_count
            call_count += 1
            return "context"

        def llm_router(messages):
            combined = "\n".join(m["content"] for m in messages)
            if "search queries" in combined and "JSON array" in combined:
                return (
                    '[{"query": "q1", "researchGoal": "g1"}, '
                    '{"query": "q2", "researchGoal": "g2"}, '
                    '{"query": "q3", "researchGoal": "g3"}]'
                )
            if "follow-up questions" in combined.lower():
                return '{"questions": ["Aspect?"]}'
            if "extract key learnings" in combined:
                return '{"learnings": [{"insight": "L", "sourceUrl": ""}], "followUpQuestions": []}'
            if "evaluate whether web research" in combined:
                return '{"score": 9, "stop": true, "knowledge_gaps": [], "suggested_queries": []}'
            return "Report."

        run_deep_research(
            "topic",
            None,
            llm_chat=lambda msgs, _max: llm_router(msgs),
            run_web_agent=run_web_agent,
            stop_checker=None,
            status_callback=None,
            breadth=3,
            max_rounds=1,
            max_sub_queries=2,
            concurrency=3,
            plain_text_format="plain",
            initial_search_snippet="x",
        )
        assert call_count == 2


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
            patch("plugin.framework.config.get_config_int_safe", return_value=0),
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
            patch("plugin.framework.config.get_config_int_safe", return_value=0),
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


def test_assess_research_coverage_parses_score():
    def llm(_msgs, _max):
        return '{"score": 8, "knowledge_gaps": [], "suggested_queries": [], "stop": true}'

    out = assess_research_coverage(llm, "topic", ["finding"], {}, quality_threshold=7)
    assert out["score"] == 8.0
    assert out["stop"] is True
