# WriterAgent - tests for deep web research (adaptive loop + orchestrator)

import pytest
from unittest.mock import MagicMock, patch


from plugin.chatbot.web_research_deep import (
    ResearchProgress,
    _ResearchAccumulator,
    _process_one_sub_query,
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

    def test_sub_query_citations_contribute_urls_not_learnings(self):
        learning = "Elevators need a counterweight"
        url = "https://example.com/elevators"
        payload = (
            '{"learnings": [{"insight": "%s", "sourceUrl": "%s"}], "followUpQuestions": []}'
            % (learning, url)
        )

        def llm_chat(messages, max_tokens=0):
            return payload

        branch = _process_one_sub_query(
            {"query": "elevator physics", "researchGoal": "mass"},
            run_web_agent=lambda query, goal, params: "prose without a url",
            llm_chat=llm_chat,
            stop_checker=lambda: False,
            acc=_ResearchAccumulator(),
            max_sub_queries=3,
            progress=ResearchProgress(),
            status_callback=None,
            on_progress=None,
        )
        assert branch is not None
        assert url in branch["sources"]
        assert learning not in branch["sources"]

    def test_research_progress_status_text(self):
        p = ResearchProgress(current_round=2, max_rounds=3, completed_queries=5, max_sub_queries=14, current_query="climate policy")
        assert "round 2/3" in p.status_text()
        assert "5/14" in p.status_text()


class TestRunDeepResearch:
    def _llm_router(self, messages):
        combined = "\n".join(m["content"] for m in messages)
        if "search queries" in combined and "JSON array" in combined:
            return '[{"query": "sub one", "researchGoal": "goal one"}]'
        if "extract key learnings" in combined:
            return '{"learnings": [{"insight": "Finding X", "sourceUrl": "https://a.test"}], "followUpQuestions": []}'
        if "follow-up questions" in combined.lower() or ('"questions"' in combined and "JSON object" in combined):
            return '{"questions": ["Aspect A?"]}'
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

    def test_all_sub_queries_fail_returns_error_not_report(self):
        def run_web_agent(_sub_query, _goal, _history):
            return {"status": "error", "code": "TOOL_EXECUTION_ERROR", "message": "fetch failed"}

        result = run_deep_research(
            "topic",
            None,
            llm_chat=lambda msgs, _max: self._llm_router(msgs),
            run_web_agent=run_web_agent,
            stop_checker=None,
            status_callback=None,
            breadth=1,
            max_rounds=1,
            max_sub_queries=3,
            plain_text_format="plain",
            initial_search_snippet="preview",
        )
        assert isinstance(result, dict)
        assert result.get("status") == "error"
        assert "fetch failed" in str(result.get("message") or "")

    def test_partial_sub_query_failure_still_synthesizes(self):
        def run_web_agent(sub_query, _goal, _history):
            if sub_query == "q1":
                raise RuntimeError("branch exploded")
            return "Sub-agent context for survivor"

        def llm_router(messages):
            combined = "\n".join(m["content"] for m in messages)
            if "search queries" in combined and "JSON array" in combined:
                return (
                    '[{"query": "q1", "researchGoal": "g1"}, '
                    '{"query": "q2", "researchGoal": "g2"}]'
                )
            return self._llm_router(messages)

        result = run_deep_research(
            "topic",
            None,
            llm_chat=lambda msgs, _max: llm_router(msgs),
            run_web_agent=run_web_agent,
            stop_checker=None,
            status_callback=None,
            breadth=2,
            max_rounds=1,
            max_sub_queries=5,
            concurrency=2,
            plain_text_format="plain",
            initial_search_snippet="preview",
        )
        assert result == "Final synthesized report."

    def test_user_stopped_during_sub_agent_returns_stop_payload(self):
        def run_web_agent(_sub_query, _goal, _history):
            return {
                "status": "error",
                "code": "USER_STOPPED",
                "message": "Web search stopped by user.",
            }

        result = run_deep_research(
            "topic",
            None,
            llm_chat=lambda msgs, _max: self._llm_router(msgs),
            run_web_agent=run_web_agent,
            stop_checker=None,
            status_callback=None,
            breadth=1,
            max_rounds=1,
            max_sub_queries=3,
            plain_text_format="plain",
            initial_search_snippet="preview",
        )
        assert isinstance(result, dict)
        assert result.get("status") == "error"
        assert result.get("code") == "USER_STOPPED"

    def test_synthesis_failure_returns_collected_learnings(self):
        def llm_router(messages):
            combined = "\n".join(m["content"] for m in messages)
            if "plain-text research report" in combined or "Collected evidence" in combined:
                raise TimeoutError("synthesis timed out")
            return self._llm_router(messages)

        def run_web_agent(sub_query, research_goal, _history):
            return "Sub-agent context for " + sub_query

        result = run_deep_research(
            "main topic",
            None,
            llm_chat=lambda msgs, _max: llm_router(msgs),
            run_web_agent=run_web_agent,
            stop_checker=None,
            status_callback=None,
            breadth=1,
            max_rounds=1,
            max_sub_queries=5,
            plain_text_format="Use plain text.",
            initial_search_snippet="preview hit",
        )
        assert isinstance(result, str)
        assert "Finding X" in result
        assert "https://a.test" in result
        assert "synthesis failed" in result.lower() or "automatic synthesis" in result.lower()

    def test_user_stopped_during_extraction_is_not_partial_success(self):
        from plugin.framework.errors import ToolExecutionError

        extract_calls = {"n": 0}
        synth_calls = {"n": 0}

        def llm_router(messages):
            combined = "\n".join(m["content"] for m in messages)
            if "extract key learnings" in combined:
                extract_calls["n"] += 1
                if extract_calls["n"] == 1:
                    raise ToolExecutionError("Web search stopped by user.", code="USER_STOPPED")
                return '{"learnings": [{"insight": "Finding X", "sourceUrl": "https://a.test"}], "followUpQuestions": []}'
            if "plain-text research report" in combined or "Collected evidence" in combined:
                synth_calls["n"] += 1
                return "partial report that must not be cached"
            return self._llm_router(messages)

        def query_router(messages):
            combined = "\n".join(m["content"] for m in messages)
            if "search queries" in combined and "JSON array" in combined:
                return (
                    '[{"query": "q1", "researchGoal": "g1"}, '
                    '{"query": "q2", "researchGoal": "g2"}]'
                )
            return llm_router(messages)

        result = run_deep_research(
            "topic",
            None,
            llm_chat=lambda msgs, _max: query_router(msgs),
            run_web_agent=lambda *_args: "sub context",
            stop_checker=None,
            status_callback=None,
            breadth=2,
            max_rounds=1,
            max_sub_queries=5,
            concurrency=1,
            plain_text_format="plain",
            initial_search_snippet="preview",
        )
        assert isinstance(result, dict)
        assert result.get("status") == "error"
        assert result.get("code") == "USER_STOPPED"
        assert synth_calls["n"] == 0
        assert "partial report" not in str(result.get("message") or "")

    def test_user_stopped_during_synthesis_is_not_partial_report(self):
        from plugin.framework.errors import ToolExecutionError

        def llm_router(messages):
            combined = "\n".join(m["content"] for m in messages)
            if "plain-text research report" in combined or "Collected evidence" in combined:
                raise ToolExecutionError("Web search stopped by user.", code="USER_STOPPED")
            return self._llm_router(messages)

        result = run_deep_research(
            "main topic",
            None,
            llm_chat=lambda msgs, _max: llm_router(msgs),
            run_web_agent=lambda sub_query, _goal, _history: "Sub-agent context for " + sub_query,
            stop_checker=None,
            status_callback=None,
            breadth=1,
            max_rounds=1,
            max_sub_queries=5,
            plain_text_format="Use plain text.",
            initial_search_snippet="preview hit",
        )
        assert isinstance(result, dict)
        assert result.get("code") == "USER_STOPPED"
        assert "Finding X" not in str(result.get("message") or "")
        assert "automatic synthesis" not in str(result.get("message") or "").lower()


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


def _frozen_clock_stamp():
    from datetime import datetime, timezone

    frozen = datetime(2026, 8, 24, 19, 14, 5, tzinfo=timezone.utc)
    return frozen, frozen.strftime("%A, %Y-%m-%d %H:%M:%S")


def test_generate_search_queries_includes_local_clock_stamp():
    from plugin.chatbot.web_research_deep import generate_search_queries

    captured: dict[str, str] = {}

    def fake_llm(messages, max_tokens):
        captured["user"] = messages[-1]["content"]
        return '[{"query": "q1", "researchGoal": "g1"}, {"query": "q2", "researchGoal": "g2"}]'

    frozen, stamp = _frozen_clock_stamp()
    with patch("plugin.chatbot.web_research_deep.now_aware", return_value=frozen):
        out = generate_search_queries(fake_llm, "some topic", 2)

    assert len(out) == 2
    assert "up-to-date" in captured["user"]
    assert f"Current time: {stamp}" in captured["user"]
    assert "Today's date is" not in captured["user"]


def test_generate_research_plan_uses_llm_client_datetime_format():
    from plugin.chatbot.web_research_deep import generate_research_plan

    captured: dict[str, str] = {}

    def fake_llm(messages, max_tokens):
        captured["user"] = messages[-1]["content"]
        return '{"questions": ["q1", "q2"]}'

    frozen, stamp = _frozen_clock_stamp()
    with patch("plugin.chatbot.web_research_deep.now_aware", return_value=frozen):
        out = generate_research_plan(fake_llm, "some topic", "snippet", num_questions=2)

    assert len(out) == 2
    assert f"Current time: {stamp}" in captured["user"]


def test_chat_completion_sync_stop_checker_not_finish_reason():
    from plugin.framework.client.llm_client import LlmClient
    from plugin.framework.errors import ToolExecutionError

    client = LlmClient.__new__(LlmClient)
    client._stopped = False
    messages = [{"role": "user", "content": "hi"}]
    seen: dict[str, object] = {}

    def fake_request(*_args, **kwargs):
        seen.update(kwargs)
        return {"role": "assistant", "content": "done", "finish_reason": "stop"}

    client.request_with_tools = fake_request

    def not_stopped() -> bool:
        return False

    def stopped() -> bool:
        return True

    assert client.chat_completion_sync(messages) == "done"
    assert client.chat_completion_sync(messages, stop_checker=not_stopped) == "done"
    assert seen.get("stop_checker") is not_stopped

    with pytest.raises(ToolExecutionError) as raised:
        client.chat_completion_sync(messages, stop_checker=stopped)
    assert raised.value.code == "USER_STOPPED"

    # The latch alone is not a stop for callers that never passed a checker.
    client._stopped = True
    assert client.chat_completion_sync(messages) == "done"
    with pytest.raises(ToolExecutionError) as raised_latch:
        client.chat_completion_sync(messages, stop_checker=not_stopped)
    assert raised_latch.value.code == "USER_STOPPED"


def _deep_preview_params(approval_callback, *, prompt: bool):
    from plugin.chatbot.web_research import WebAgentRunParams

    api = MagicMock()
    api.config = {}
    api.ctx = object()
    model = MagicMock()
    model.api = api
    model.max_tokens = 32
    return WebAgentRunParams(
        smol_model=model,
        max_steps=4,
        cache_path=None,
        cache_max_mb=0,
        cache_max_age_days=30,
        cdp_enabled=False,
        cdp_url=None,
        stop_checker=None,
        status_callback=None,
        append_thinking_callback=None,
        approval_callback=approval_callback,
        chat_append_callback=None,
        prompt_for_web_research=prompt,
        outer_query="topic",
        cancellation_scope=object(),
    )


def test_deep_preview_reject_skips_fetch_and_research_continues():
    from plugin.chatbot.web_research import _run_deep_web_research

    for decision in (False, (False, None)):
        params = _deep_preview_params(lambda q, tool, args, decision=decision: decision, prompt=True)
        with patch("plugin.contrib.smolagents.default_tools.DuckDuckGoSearchTool") as ddg, \
             patch("plugin.chatbot.web_research_deep.run_deep_research", return_value="report") as run_deep, \
             patch("plugin.framework.config.get_config_int", return_value=1), \
             patch("plugin.framework.config.get_config_int_safe", return_value=1):
            out = _run_deep_web_research(
                MagicMock(), "topic", None, params,
                cache_path=None, cache_max_mb=0, cache_max_age_days=30, plain_text_format="plain",
            )
        assert out == "report"
        ddg.assert_not_called()
        assert run_deep.call_args.kwargs["initial_search_snippet"] == ""
        assert callable(run_deep.call_args.kwargs["worker_factory"])


def test_deep_preview_approval_uses_edited_query():
    from plugin.chatbot.web_research import _run_deep_web_research

    def approval(query, tool, args):
        assert tool == "web_search"
        assert query == "topic"
        return True, "edited topic"

    params = _deep_preview_params(approval, prompt=True)
    with patch("plugin.contrib.smolagents.default_tools.DuckDuckGoSearchTool") as ddg, \
         patch("plugin.chatbot.web_research_deep.run_deep_research", return_value="report") as run_deep, \
         patch("plugin.framework.config.get_config_int", return_value=1), \
         patch("plugin.framework.config.get_config_int_safe", return_value=1):
        ddg.return_value.forward.return_value = "snippet text"
        out = _run_deep_web_research(
            MagicMock(), "topic", None, params,
            cache_path=None, cache_max_mb=0, cache_max_age_days=30, plain_text_format="plain",
        )
    assert out == "report"
    ddg.return_value.forward.assert_called_once_with("edited topic")
    assert run_deep.call_args.kwargs["initial_search_snippet"] == "snippet text"


class _RecordingClient:
    instances: list["_RecordingClient"] = []

    def __init__(self, config, ctx, cancellation_scope=None):
        self.config = config
        self.ctx = ctx
        self.cancellation_scope = cancellation_scope
        self._stopped = False
        self.kinds: list[str] = []
        self.saw_stop_checker = False
        _RecordingClient.instances.append(self)

    def chat_completion_sync(self, messages, max_tokens=512, **kwargs):
        self.saw_stop_checker = "stop_checker" in kwargs
        combined = "\n".join(str(m.get("content") or "") for m in messages)
        if "extract key learnings" in combined:
            self.kinds.append("extract")
            if _RecordingClient.extract_raises:
                from plugin.framework.errors import ToolExecutionError

                raise ToolExecutionError("Web search stopped by user.", code="USER_STOPPED")
            return '{"learnings": [{"insight": "Finding X", "sourceUrl": "https://a.test"}], "followUpQuestions": []}'
        if "plain-text research report" in combined or "Collected evidence" in combined:
            self.kinds.append("synth")
            if _RecordingClient.synth_raises:
                from plugin.framework.errors import ToolExecutionError

                raise ToolExecutionError("Web search stopped by user.", code="USER_STOPPED")
            return "Final synthesized report."
        if "search queries" in combined and "JSON array" in combined:
            self.kinds.append("queries")
            return '[{"query": "sub one", "researchGoal": "goal one"}]'
        if "evaluate whether web research" in combined or "Quality threshold" in combined:
            self.kinds.append("assess")
            return '{"score": 9, "knowledge_gaps": [], "suggested_queries": [], "stop": true}'
        if '"questions"' in combined and "JSON object" in combined:
            self.kinds.append("plan")
            return '{"questions": ["Aspect A?"]}'
        self.kinds.append("other")
        return "{}"


_RecordingClient.extract_raises = False
_RecordingClient.synth_raises = False


def _execute_deep_with_recording_client(tmp_path, *, extract_raises: bool, synth_raises: bool):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    _RecordingClient.instances = []
    _RecordingClient.extract_raises = extract_raises
    _RecordingClient.synth_raises = synth_raises
    ctx = MagicMock()
    ctx.ctx = MockContext()
    ctx.doc = None
    ctx.status_callback = None
    ctx.append_thinking_callback = None
    ctx.approval_callback = None
    ctx.chat_append_callback = None
    ctx.stop_checker = lambda: False
    scope = object()
    ctx.send_cancellation = scope

    def _cfg_int(key):
        if key == "web_cache_validity_days":
            return 30
        if "breadth" in key:
            return 1
        if key == "web_cache_max_mb":
            return 50
        return 8

    def _cfg_int_safe(key):
        if "max_rounds" in key:
            return 1
        if "sub_agent" in key:
            return 0
        return 50

    with patch("plugin.framework.config.get_config_bool_safe", return_value=True), \
         patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_config_int", side_effect=_cfg_int), \
         patch("plugin.framework.config.get_config_int_safe", side_effect=_cfg_int_safe), \
         patch("plugin.framework.config.get_api_config", return_value={"model": "test-model"}), \
         patch("plugin.framework.config.get_config", return_value="off"), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.web_research_cache._research_cache_embedding_configured", return_value=False), \
         patch("plugin.framework.client.llm_client.LlmClient", _RecordingClient), \
         patch("plugin.chatbot.web_research._run_web_agent", return_value="sub context"), \
         patch("plugin.contrib.smolagents.default_tools.DuckDuckGoSearchTool") as ddg:
        ddg.return_value.forward.return_value = "preview"
        result = WebResearchTool().execute(ctx, query="Execute a new search query", deep=True)
    return result, scope


def test_user_stopped_during_sub_query_or_synthesis_is_not_cached(tmp_path):
    from plugin.contrib.smolagents.default_tools import _web_cache_get, _web_cache_list_keys

    db_file = str(tmp_path / "writeragent_web_cache.db")
    for extract_raises, synth_raises in ((True, False), (False, True)):
        result, scope = _execute_deep_with_recording_client(tmp_path, extract_raises=extract_raises, synth_raises=synth_raises)
        assert result.get("status") == "error"
        assert result.get("code") == "USER_STOPPED"
        assert "Finding X" not in str(result.get("result") or "")
        assert "automatic synthesis" not in str(result.get("message") or "").lower()
        assert _web_cache_list_keys(db_file, "research", 30) == []
        assert _web_cache_get(db_file, "research", "deep|english|execute", max_age_days=30) is None
        assert _web_cache_get(db_file, "research", "english|execute", max_age_days=30) is None

        instances = _RecordingClient.instances
        assert len(instances) >= 2
        parent = instances[0]
        workers = instances[1:]
        assert parent.cancellation_scope is scope
        assert all(worker.cancellation_scope is scope for worker in workers)
        assert all(worker.config is parent.config for worker in workers)
        assert "extract" not in parent.kinds
        assert any("extract" in worker.kinds for worker in workers)
        assert parent.saw_stop_checker
        if synth_raises:
            assert "synth" in parent.kinds
        else:
            assert "synth" not in parent.kinds
            assert all("synth" not in worker.kinds for worker in workers)
