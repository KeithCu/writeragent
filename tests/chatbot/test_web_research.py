# WriterAgent - combined tests for web research functionality

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from plugin.chatbot.web_research import (
    _apply_web_search_query_override,
    _norm_research_query,
    _web_search_query_from_arguments,
    WebResearchToolCallingAgent,
)
from plugin.chatbot.web_research_chat import (
    web_research_cache_chat_text,
    web_research_engine_chat_block,
    web_search_engine_step_chat_text,
)
from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, VisitWebpageTool
from plugin.contrib.smolagents.utils import AgentParsingError
from plugin.contrib.smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    Model,
    TokenUsage,
)
from plugin.framework.constants import get_plugin_dir
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("requests")
import requests


# =============================================================================
# Default OpenRouter model for the search sub-agent CLI
# =============================================================================

DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3-nano-30b-a3b"


# =============================================================================
# Project root path setup for CLI
# =============================================================================


def _add_project_root_to_path() -> None:
    """Ensure the project root is on sys.path when run via `python -m`."""
    project_root = os.path.dirname(get_plugin_dir())
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


_add_project_root_to_path()


# =============================================================================
# OpenRouterSmolModel - Minimal smolagents Model for OpenRouter
# =============================================================================


class OpenRouterSmolModel(Model):
    """
    Minimal smolagents `Model` implementation that talks directly to OpenRouter.

    It implements only the `generate` method, sufficient for ToolCallingAgent +
    search_web use. Streaming is not implemented here.
    """

    def __init__(
        self,
        api_key: str,
        model_id: str,
        max_tokens: int = 1024,
        endpoint: str = "https://openrouter.ai/api/v1/chat/completions",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.endpoint = endpoint

    def _to_openai_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Convert smolagents ChatMessage list to OpenAI-style messages."""
        result: list[dict[str, Any]] = []
        for m in messages:
            content = m.content
            if isinstance(content, list):
                # ToolCallingAgent prompts use text-only content; flatten it.
                text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                text = "\n".join([t for t in text_parts if t])
            else:
                text = str(content or "")
            result.append({"role": m.role.value, "content": text})
        return result

    def _to_openai_tools(self, tools: dict[str, Any] | None) -> list[dict[str, Any]] | None:
        """
        Convert smolagents Tool objects to OpenAI `tools` schema.

        ToolCallingAgent already encodes everything needed on the Tool side via
        `to_tool_calling_prompt`, so here we only need the JSON schema, which
        smolagents models._prepare_completion_kwargs knows how to build. That
        method passes us a `tools` list already, so we simply forward it.
        """
        if tools is None:
            return None
        # When called via Model._prepare_completion_kwargs we already get an
        # OpenAI-style `tools` list, so just return it.
        if isinstance(tools, list):
            return tools
        return None

    def generate(
        self,
        messages: list[ChatMessage],
        stop_sequences: list[str] | None = None,
        tools_to_call_from=None,
        **kwargs: Any,
    ) -> ChatMessage:
        """
        Synchronous completion call to OpenRouter, with optional tool-calling.
        """
        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            tools_to_call_from=tools_to_call_from,
            **kwargs,
        )

        openai_messages = completion_kwargs.get("messages", [])
        tools = completion_kwargs.get("tools")

        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": openai_messages,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stop_sequences:
            payload["stop"] = stop_sequences

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        started = time.time()
        resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=120)
        elapsed = time.time() - started
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenRouter error {resp.status_code}: {resp.text}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices: {json.dumps(data)[:500]}")
        choice = choices[0]
        message_dict = choice.get("message", {})

        content = message_dict.get("content") or ""
        tool_calls_raw = message_dict.get("tool_calls") or []

        tool_calls: list[ChatMessageToolCall] = []
        for tc in tool_calls_raw:
            func = tc.get("function", {}) or {}
            tool_calls.append(
                ChatMessageToolCall(
                    id=tc.get("id", "call_0"),
                    type=tc.get("type", "function"),
                    function=ChatMessageToolCallFunction(
                        name=func.get("name", ""),
                        arguments=func.get("arguments", "") or "",
                    ),
                )
            )

        usage_dict = data.get("usage") or {}
        token_usage = None
        if usage_dict:
            token_usage = TokenUsage(
                input_tokens=usage_dict.get("prompt_tokens", 0),
                output_tokens=usage_dict.get("completion_tokens", 0),
            )

        msg = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls or None,
        )
        if token_usage is not None:
            msg.token_usage = token_usage

        # Simple debug print for manual runs
        sys.stderr.write(
            f"[OpenRouter] tokens_in={usage_dict.get('prompt_tokens', 0)} "
            f"tokens_out={usage_dict.get('completion_tokens', 0)} "
            f"elapsed={elapsed:.2f}s\n"
        )
        return msg


# =============================================================================
# CLI main for search_web
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test the smolagents-based web search agent via OpenRouter.")
    parser.add_argument("query", help="Natural language question to research on the web.")
    parser.add_argument("--max-tokens", type=int, default=2048, help="Max tokens for the sub-agent model.")
    parser.add_argument(
        "--model",
        default=DEFAULT_OPENROUTER_MODEL,
        help=f"OpenRouter model id (default: {DEFAULT_OPENROUTER_MODEL}).",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.stderr.write("Error: OPENROUTER_API_KEY environment variable is required.\n")
        return 1

    model = OpenRouterSmolModel(api_key=api_key, model_id=args.model, max_tokens=args.max_tokens)
    from plugin.framework.config import get_config_int
    from plugin.tests.testing_utils import MockContext

    cache_max_age_days = get_config_int(MockContext(), "web_cache_validity_days")
    tools = [DuckDuckGoSearchTool(cache_max_age_days=cache_max_age_days), VisitWebpageTool(cache_max_age_days=cache_max_age_days)]
    agent = ToolCallingAgent(tools=tools, model=model, stream_outputs=False)

    task = (
        "Please find the answer to this query by searching the web and reading pages if needed. "
        "When you are confident in the answer, call the final_answer tool with a concise natural-language response.\n\n"
        f"Query: {args.query}"
    )

    sys.stderr.write(f"[search_web CLI] Running sub-agent on query: {args.query!r}\n")
    try:
        answer = agent.run(task)
        # ToolCallingAgent returns the final answer string by default.
        print(str(answer).strip())
        return 0
    except AgentParsingError:
        # Some models may ignore tool-calling and just return plain text without
        # a JSON tool-call blob. In that case, fall back to a single direct
        # completion so the CLI still produces a useful answer.
        sys.stderr.write(
            "[search_web CLI] Model output could not be parsed as tool calls; "
            "falling back to a plain completion.\n"
        )
        messages = [
            ChatMessage(
                role=MessageRole.USER,
                content=[{"type": "text", "text": task}],
            )
        ]
        msg = model.generate(messages)
        print(str(msg.content or "").strip())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())


# =============================================================================
# Web research chat block formatting tests (from test_web_research_chat.py)
# =============================================================================


def test_step_zero_matches_legacy_engine_block():
    q = "best pizza test"
    a = web_search_engine_step_chat_text(q, 0)
    b = web_research_engine_chat_block(q)
    assert a == b
    assert "Tool: web_search" in a
    assert "best pizza test" in a
    assert "[Web search]" not in a
    assert "[Additional web search]" not in a


def test_step_one_same_format_as_first():
    q = "refined query"
    first = web_search_engine_step_chat_text(q, 0)
    second = web_search_engine_step_chat_text(q, 1)
    assert first == second
    assert "Tool: web_search" in second
    assert "[Additional web search]" not in second
    assert "[Web search]" not in second


def test_web_research_engine_chat_block_ignores_legacy_approval_flag():
    q = "x"
    assert web_research_engine_chat_block(q, approval_required=True) == web_search_engine_step_chat_text(q, 0)
    assert "approval required" not in web_research_engine_chat_block(q, approval_required=True).lower()


def test_step_index_negative_treated_as_first():
    q = "q"
    assert web_search_engine_step_chat_text(q, -1) == web_search_engine_step_chat_text(q, 0)


def test_norm_research_query_collapses_whitespace_and_case():
    assert _norm_research_query("  Best  Pizza \n") == _norm_research_query("best pizza")
    assert _norm_research_query("") == ""


def test_format_research_cache_result_chat_from_payload():
    from plugin.chatbot.web_research_chat import format_research_cache_result_chat

    assert format_research_cache_result_chat({}) == ""
    block = format_research_cache_result_chat({
        "research_cache_event": "hit",
        "research_cache_key": "heights madison pizza",
    })
    assert "Research cache hit" in block
    assert "heights madison pizza" in block


def test_cache_hit_does_not_stream_chat_append_callback():
    """Cache notice is shown once via tool result / delegate line, not chat_append during execute."""
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    ctx = MagicMock()
    ctx.ctx = MockContext()
    chat_lines: list[str] = []
    ctx.chat_append_callback = chat_lines.append

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db_file = os.path.join(td, "writeragent_web_cache.db")
        _web_cache_set(db_file, "research", "caching unique", "Cached Answer Content", 50 * 1024 * 1024)

        def _cfg_int(key):
            if key == "web_cache_validity_days":
                return 30
            if key == "web_research_cache_jaccard_percent":
                return 40
            if key == "web_research_cache_min_overlap":
                return 8
            return 50

        with patch("plugin.framework.config.get_config_bool_safe", return_value=True), \
             patch("plugin.framework.config.user_config_dir", return_value=td), \
             patch("plugin.framework.config.get_config_int_safe", return_value=50), \
             patch("plugin.framework.config.get_config_int", side_effect=_cfg_int), \
             patch("plugin.framework.prompts.should_prepend_dev_llm_system_prefix", return_value=False), \
             patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")):
            tool = WebResearchTool()
            res = tool.execute(ctx, query="Search for caching test unique info")
            assert res.get("research_cache_event") == "hit"
            assert chat_lines == []


def test_format_delegate_result_includes_research_cache():
    from plugin.chatbot.tool_loop_state import format_delegate_result_chat_line

    line = format_delegate_result_chat_line(
        {"domain": "web_research", "task": "pizza"},
        {
            "status": "ok",
            "research_cache_event": "saved",
            "research_cache_key": "execute",
        },
    )
    assert "Research cache saved" in line
    assert "execute" in line
    assert "[delegate (web_research): done]" in line


def test_web_research_cache_chat_text_hit_and_saved():
    hit = web_research_cache_chat_text({
        "research_cache_event": "hit",
        "research_cache_key": "heights madison pizza",
    })
    assert "Research cache hit" in hit
    assert "heights madison pizza" in hit
    saved = web_research_cache_chat_text({
        "research_cache_event": "saved",
        "research_cache_key": "execute",
    })
    assert "Research cache saved" in saved
    assert "execute" in saved


def test_web_research_cache_chat_text_fuzzy_hit():
    block = web_research_cache_chat_text({
        "research_cache_event": "hit_fuzzy",
        "research_cache_key": "elevator space",
        "research_cache_matched_key": "english|elevator physics",
        "research_cache_lang": "english",
        "research_cache_jaccard": 0.65,
    })
    assert "fuzzy" in block.lower()
    assert "65%" in block
    assert "elevator space" in block


def test_web_research_cache_chat_text_embedding_hit():
    block = web_research_cache_chat_text({
        "research_cache_event": "hit_embedding",
        "research_cache_key": "elevator space",
        "research_cache_matched_key": "english|space elevator physics",
        "research_cache_lang": "english",
        "research_cache_similarity": 0.82,
    })
    assert "embedding" in block.lower()
    assert "82%" in block
    assert "elevator space" in block


# =============================================================================
# Web research query override tests (from test_web_research_query_override.py)
# =============================================================================


def test_web_search_query_from_dict():
    assert _web_search_query_from_arguments({"query": "hello"}) == "hello"
    assert _web_search_query_from_arguments({}) == ""


def test_web_search_query_from_json_string():
    assert _web_search_query_from_arguments('{"query": "from json"}') == "from json"


def test_web_search_query_from_invalid_string():
    assert _web_search_query_from_arguments("not json") == ""
    assert _web_search_query_from_arguments(None) == ""


def test_apply_override_dict_mutable():
    step = SimpleNamespace(arguments={"query": "old", "x": 1})
    assert _apply_web_search_query_override(step, "new") is False
    assert step.arguments == {"query": "new", "x": 1}


def test_apply_override_json_string():
    step = SimpleNamespace(arguments='{"query": "old"}')
    assert _apply_web_search_query_override(step, "edited") is True
    assert step.arguments == {"query": "edited"}


def test_apply_override_invalid_json_string():
    step = SimpleNamespace(arguments="<<<")
    assert _apply_web_search_query_override(step, "only-override") is True
    assert step.arguments == {"query": "only-override"}


def test_apply_override_non_dict_non_string():
    step = SimpleNamespace(arguments=["list"])
    assert _apply_web_search_query_override(step, "fallback") is True
    assert step.arguments == {"query": "fallback"}


# =============================================================================
# Web search approval chat preview tests
# =============================================================================


def _run_web_search_tool_call_handler(
    *,
    outer_query: str = "user query",
    search_query: str = "engine query",
    prompt_for_web_research: bool = False,
    approval_callback=None,
    chat_lines: list[str] | None = None,
):
    """Invoke only the web_search branch of _run_web_agent's tool_call_handler."""
    from plugin.chatbot.web_research import WebAgentRunParams, _run_web_agent
    from plugin.contrib.smolagents.memory import ToolCall

    captured_chat = chat_lines if chat_lines is not None else []
    handler_results: list[Any] = []

    def _chat_append(text: str) -> None:
        captured_chat.append(text)

    params = WebAgentRunParams(
        smol_model=MagicMock(),
        max_steps=5,
        cache_path=None,
        cache_max_mb=0,
        cache_max_age_days=30,
        cdp_enabled=False,
        cdp_url=None,
        stop_checker=lambda: False,
        status_callback=None,
        append_thinking_callback=None,
        approval_callback=approval_callback,
        chat_append_callback=_chat_append,
        prompt_for_web_research=prompt_for_web_research,
        outer_query=outer_query,
    )

    def fake_execute_safe(agent, task, tool_call_handler=None, **kwargs):
        if tool_call_handler:
            step = ToolCall(name="web_search", arguments={"query": search_query}, id="t1")
            handler_results.append(tool_call_handler(step))
        return "done"

    with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exe_cls, \
         patch("plugin.chatbot.web_research.WebResearchToolCallingAgent"), \
         patch("plugin.chatbot.smol_examples.get_examples_block", return_value=""), \
         patch("plugin.contrib.smolagents.default_tools.DuckDuckGoSearchTool"), \
         patch("plugin.contrib.smolagents.default_tools.VisitWebpageTool"), \
         patch("plugin.chatbot.web_research._VisitWebpageDedupTool"):
        mock_exe_cls.return_value.execute_safe.side_effect = fake_execute_safe
        _run_web_agent(MagicMock(), outer_query, None, params)

    return captured_chat, handler_results


def test_web_search_preview_shown_before_approval_returns():
    chat_lines: list[str] = []
    approval_called_after_chat: list[bool] = []

    def approval_cb(query_for_engine, tool, args):
        approval_called_after_chat.append(len(chat_lines) > 0 and query_for_engine in chat_lines[0])
        return True, None

    chat_lines, _ = _run_web_search_tool_call_handler(
        outer_query="What is Python?",
        search_query="latest Python release",
        prompt_for_web_research=True,
        approval_callback=approval_cb,
        chat_lines=chat_lines,
    )
    assert approval_called_after_chat == [True]
    assert len(chat_lines) == 1
    assert "latest Python release" in chat_lines[0]
    assert "Tool: web_search" in chat_lines[0]


def test_web_search_reject_keeps_preview_in_chat():
    chat_lines: list[str] = []

    def approval_cb(query_for_engine, tool, args):
        return False, None

    chat_lines, results = _run_web_search_tool_call_handler(
        search_query="blocked query",
        prompt_for_web_research=True,
        approval_callback=approval_cb,
        chat_lines=chat_lines,
    )
    assert len(chat_lines) == 1
    assert "blocked query" in chat_lines[0]
    assert results and results[0].get("code") == "USER_STOPPED"


def test_web_search_change_appends_second_preview():
    chat_lines: list[str] = []

    def approval_cb(query_for_engine, tool, args):
        return True, "edited query"

    chat_lines, _ = _run_web_search_tool_call_handler(
        search_query="original query",
        prompt_for_web_research=True,
        approval_callback=approval_cb,
        chat_lines=chat_lines,
    )
    assert len(chat_lines) == 2
    assert "original query" in chat_lines[0]
    assert "edited query" in chat_lines[1]


def test_web_search_no_prompt_dedup_when_matches_outer_query():
    chat_lines, _ = _run_web_search_tool_call_handler(
        outer_query="best pizza madison",
        search_query="best pizza madison",
        prompt_for_web_research=False,
        chat_lines=[],
    )
    assert chat_lines == []


def test_web_search_prompt_shows_preview_even_when_matches_outer_query():
    chat_lines, _ = _run_web_search_tool_call_handler(
        outer_query="best pizza madison",
        search_query="best pizza madison",
        prompt_for_web_research=True,
        approval_callback=lambda q, tool, args: (True, None),
        chat_lines=[],
    )
    assert len(chat_lines) == 1
    assert "best pizza madison" in chat_lines[0]


# =============================================================================
# Web research step budget tests (from test_web_research_step_budget.py)
# =============================================================================


def test_web_research_agent_instructions_include_response_format():
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.framework.prompts import WEB_RESEARCH_PLAIN_TEXT_FORMAT
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())
    captured: dict = {}

    class _CaptureAgent:
        def __init__(self, **kwargs):
            captured["instructions"] = kwargs.get("instructions", "")

    with patch("plugin.chatbot.web_research.WebResearchToolCallingAgent", _CaptureAgent):
        with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:
            mock_exec.return_value.execute_safe.return_value = "answer"
            with patch("plugin.framework.config.get_config", return_value="false"):
                def _cfg_int(key):
                    if key == "web_cache_max_mb":
                        return 0
                    if key == "chat_max_tokens":
                        return 2048
                    return 10

                with patch("plugin.framework.config.get_config_int", side_effect=_cfg_int):
                    with patch("plugin.framework.config.get_api_config", return_value={}):
                        with patch("plugin.framework.config.user_config_dir", return_value="/tmp"):
                            WebResearchTool().execute(ctx, query="test query")

    assert WEB_RESEARCH_PLAIN_TEXT_FORMAT in captured["instructions"]
    assert "plain text only" in captured["instructions"]
    assert "final_answer" in captured["instructions"]
    assert "No HTML tags" in captured["instructions"]


def test_web_research_augment_messages_includes_used_and_remaining():
    model = MagicMock()
    agent = WebResearchToolCallingAgent(tools=[], model=model, max_steps=10)
    agent.step_number = 3

    # Test case 1: Appending when last message is NOT a user message
    base = [ChatMessage(role=MessageRole.SYSTEM, content="sys")]
    out = agent.augment_messages_for_step(base)
    assert len(out) == 2
    last = out[-1]
    assert last.role == MessageRole.USER
    text = str(last.content)
    assert "2 step(s) used" in text
    assert "8 step(s) remaining" in text
    assert "maximum 10" in text

    # Test case 2: Merging when last message IS a user message
    base2 = [ChatMessage(role=MessageRole.USER, content="prior")]
    out2 = agent.augment_messages_for_step(base2)
    assert len(out2) == 1
    text2 = str(out2[0].content)
    assert "2 step(s) used" in text2
    assert "prior" in text2


def test_tool_calling_agent_default_augment_is_identity():
    model = MagicMock()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=5)
    base = [ChatMessage(role=MessageRole.SYSTEM, content=[{"type": "text", "text": "sys"}])]
    assert agent.augment_messages_for_step(base) is base


def test_web_research_agent_instructions_include_minimal_tool_use_advice():
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())
    captured: dict = {}

    class _CaptureAgent:
        def __init__(self, **kwargs):
            captured["instructions"] = kwargs.get("instructions", "")

    # Test case 1: max_steps = 25 (greater than 20) -> should include advice
    with patch("plugin.chatbot.web_research.WebResearchToolCallingAgent", _CaptureAgent):
        with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:
            mock_exec.return_value.execute_safe.return_value = "answer"
            with patch("plugin.framework.config.get_config", return_value="false"):
                def _cfg_int(key):
                    if key == "web_cache_max_mb":
                        return 0
                    if key == "chat_max_tokens":
                        return 2048
                    return 25  # chatbot.max_tool_rounds

                with patch("plugin.framework.config.get_config_int", side_effect=_cfg_int):
                    with patch("plugin.framework.config.get_api_config", return_value={}):
                        with patch("plugin.framework.config.user_config_dir", return_value="/tmp"):
                            with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
                                WebResearchTool().execute(ctx, query="test query")

    assert "IMPORTANT: If the user's query is a simple question that does not require deep or extensive research" in captured["instructions"]
    assert "at most half of your step budget (i.e., 12 steps or fewer)" in captured["instructions"]
    assert "Avoid visiting Yelp (yelp.com) links" in captured["instructions"]

    # Test case 2: max_steps = 10 (less than or equal to 20) -> should NOT include advice
    captured.clear()
    with patch("plugin.chatbot.web_research.WebResearchToolCallingAgent", _CaptureAgent):
        with patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:
            mock_exec.return_value.execute_safe.return_value = "answer"
            with patch("plugin.framework.config.get_config", return_value="false"):
                def _cfg_int_low(key):
                    if key == "web_cache_max_mb":
                        return 0
                    if key == "chat_max_tokens":
                        return 2048
                    return 10  # chatbot.max_tool_rounds

                with patch("plugin.framework.config.get_config_int", side_effect=_cfg_int_low):
                    with patch("plugin.framework.config.get_api_config", return_value={}):
                        with patch("plugin.framework.config.user_config_dir", return_value="/tmp"):
                            with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
                                WebResearchTool().execute(ctx, query="test query")

    assert "IMPORTANT: If the user's query is a simple question that does not require deep or extensive research" not in captured["instructions"]
    assert "Avoid visiting Yelp (yelp.com) links" in captured["instructions"]


def test_web_research_unique_words_key():
    from plugin.chatbot.web_research import _get_embedding_words_text, _get_unique_words_key

    q = "Please find the best pizza restaurants in Madison Heights, Michigan (MI)!"
    key = _get_unique_words_key(q)
    # Fluff words like please, find, the, best, in are stripped; content words stay sorted unique.
    assert key == "heights madison michigan pizza restaurants"
    assert _get_embedding_words_text(q) == "pizza restaurants madison heights michigan"
    q2 = "Research a concise report on the best pizza in Madison Heights"
    assert _get_unique_words_key(q2) == "heights madison pizza"
    assert _get_embedding_words_text(q2) == "pizza madison heights"


def test_web_research_caching_logic(tmp_path):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()

    db_file = str(tmp_path / "writeragent_web_cache.db")

    # Set up config patches
    def _cfg_int(key):
        if key == "web_cache_validity_days":
            return 30
        if key == "web_research_cache_jaccard_percent":
            return 40
        if key == "web_research_cache_min_overlap":
            return 8
        return 50

    with patch("plugin.framework.config.get_config_bool_safe", return_value=True), \
         patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_config_int_safe", return_value=50), \
         patch("plugin.framework.config.get_config_int", side_effect=_cfg_int), \
         patch("plugin.framework.prompts.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")):

        # Pre-populate the cache using _web_cache_set
        from plugin.contrib.smolagents.default_tools import _web_cache_set
        _web_cache_set(db_file, "research", "caching unique", "Cached Answer Content", 50 * 1024 * 1024)

        tool = WebResearchTool()
        # Query that normalizes to "caching unique" ("test" is an English stop word)
        res = tool.execute(ctx, query="Search for caching test unique info")
        assert res["status"] == "ok"
        assert res["result"] == "Cached Answer Content"
        assert res.get("research_cache_event") == "hit"
        assert res.get("research_cache_key") == "caching unique"
        from plugin.chatbot.web_research_chat import format_research_cache_result_chat

        cache_chat = format_research_cache_result_chat(res)
        assert "Research cache hit" in cache_chat
        assert "caching unique" in cache_chat


def test_web_research_cache_lookup_uses_embedding_threshold(tmp_path):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext

    ctx = MagicMock()
    ctx.ctx = MockContext()
    db_file = str(tmp_path / "writeragent_web_cache.db")
    db_file_path = Path(db_file)
    db_file_path.write_bytes(b"")
    captured: dict[str, object] = {}

    def _cfg_int(key):
        if key == "web_cache_validity_days":
            return 30
        if key == "web_research_cache_jaccard_percent":
            return 60
        if key == "web_research_cache_embedding_percent":
            return 75
        if key == "web_research_cache_min_overlap":
            return 8
        return 50

    def fake_lookup(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return ("hit_embedding", "caching unique", "english|cached similar", 0.78, "Cached Answer Content")

    with patch("plugin.framework.config.get_config_bool_safe", return_value=True), \
         patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_config_int_safe", return_value=50), \
         patch("plugin.framework.config.get_config_int", side_effect=_cfg_int), \
         patch("plugin.framework.prompts.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.web_research_cache.enqueue_research_cache_embedding_backfill"), \
         patch("plugin.chatbot.web_research_cache.lookup_research_cache", side_effect=fake_lookup):
        res = WebResearchTool().execute(ctx, query="Search for caching test unique info")

    assert res["status"] == "ok"
    assert res["research_cache_event"] == "hit_embedding"
    assert captured["kwargs"]["embedding_percent"] == 75
    assert captured["kwargs"]["embedding_text"] == "caching unique"
    assert captured["args"][4] == 60


def test_web_research_caching_write(tmp_path):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from plugin.contrib.smolagents.default_tools import _web_cache_get

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())

    db_file = str(tmp_path / "writeragent_web_cache.db")

    # Ensure empty db exists by writing something and checking
    from plugin.contrib.smolagents.default_tools import _web_cache_set
    _web_cache_set(db_file, "research", "dummy", "val", 1024 * 1024)

    def _cfg_int(key):
        if key == "web_cache_validity_days":
            return 30
        if key == "web_research_cache_jaccard_percent":
            return 40
        if key == "web_research_cache_min_overlap":
            return 8
        return 50

    with patch("plugin.framework.config.get_config_bool_safe", return_value=True), \
         patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_config_int_safe", return_value=50), \
         patch("plugin.framework.config.get_config_int", side_effect=_cfg_int), \
         patch("plugin.framework.prompts.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.chatbot.web_research_cache.resolve_research_locale", return_value=("en_US", "english")), \
         patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:

        mock_exec.return_value.execute_safe.return_value = "Live Searched Output"

        tool = WebResearchTool()
        res = tool.execute(ctx, query="Execute a new search query")
        assert res["status"] == "ok"
        assert res["result"] == "Live Searched Output"

        # Check database directly (new writes use lang-prefixed keys)
        cached = _web_cache_get(db_file, "research", "english|execute", max_age_days=30)
        assert cached == "Live Searched Output"
        assert res.get("research_cache_event") == "saved"
        assert res.get("research_cache_key") == "execute"
        from plugin.chatbot.web_research_chat import format_research_cache_result_chat

        cache_chat = format_research_cache_result_chat(res)
        assert "Research cache saved" in cache_chat
        assert "execute" in cache_chat


def test_write_research_cache_enqueues_embedding_backfill(tmp_path):
    from plugin.chatbot.web_research import _write_research_cache
    from plugin.contrib.smolagents.default_tools import _web_cache_get

    ctx = MagicMock()
    ctx.ctx = object()
    db_file = str(tmp_path / "writeragent_web_cache.db")

    with patch("plugin.chatbot.web_research_cache.enqueue_research_cache_embedding_backfill") as enqueue_backfill, \
         patch("plugin.chatbot.web_research_cache.enqueue_research_cache_embedding_for_row") as enqueue_row:
        fields = _write_research_cache(ctx, db_file, "execute", "Live Searched Output", 50, 30, "english", embedding_text="execute ordered")

    assert fields["research_cache_event"] == "saved"
    assert _web_cache_get(db_file, "research", "english|execute", max_age_days=30) == "Live Searched Output"
    enqueue_row.assert_called_once_with(ctx.ctx, db_file, "english|execute", "execute ordered")
    enqueue_backfill.assert_called_once_with(ctx.ctx, db_file, 30)


def test_web_research_caching_disabled_bypasses_cache(tmp_path):
    from plugin.chatbot.web_research import WebResearchTool
    from plugin.tests.testing_utils import MockContext
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    ctx = MagicMock()
    ctx.ctx = MockContext()
    setattr(ctx.ctx, "getServiceManager", MagicMock())

    db_file = str(tmp_path / "writeragent_web_cache.db")
    _web_cache_set(db_file, "research", "caching unique", "Cached Answer Content", 50 * 1024 * 1024)

    def _cfg_int(key):
        if key == "web_cache_validity_days":
            return 30
        if key == "web_research_cache_jaccard_percent":
            return 40
        if key == "web_research_cache_min_overlap":
            return 8
        return 50

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False), \
         patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)), \
         patch("plugin.framework.config.get_config_int_safe", return_value=50), \
         patch("plugin.framework.config.get_config_int", side_effect=_cfg_int), \
         patch("plugin.framework.prompts.should_prepend_dev_llm_system_prefix", return_value=False), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.chatbot.smol_agent.SmolAgentExecutor") as mock_exec:

        mock_exec.return_value.execute_safe.return_value = "Live Searched Output"

        tool = WebResearchTool()
        res = tool.execute(ctx, query="Search for caching test unique info")
        assert res["status"] == "ok"
        assert res["result"] == "Live Searched Output"
        mock_exec.return_value.execute_safe.assert_called_once()


