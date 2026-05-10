# WriterAgent - combined tests for web research functionality

import argparse
import json
import os
import sys
import time
from types import SimpleNamespace
from typing import Any

from plugin.chatbot.web_research import (
    _apply_web_search_query_override,
    _norm_research_query,
    _web_search_query_from_arguments,
    WebResearchToolCallingAgent,
)
from plugin.chatbot.web_research_chat import (
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
    tools = [DuckDuckGoSearchTool(), VisitWebpageTool()]
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
    a = web_search_engine_step_chat_text(q, 0, approval_required=False)
    b = web_research_engine_chat_block(q, approval_required=False)
    assert a == b
    assert "[Web search]" in a
    assert "[Additional web search]" not in a


def test_step_one_is_additional_not_duplicate_full_header():
    q = "refined query"
    first = web_search_engine_step_chat_text(q, 0, approval_required=False)
    second = web_search_engine_step_chat_text(q, 1, approval_required=False)
    assert "[Additional web search]" in second
    assert second.count("[Web search]") == 0
    assert "[Web search]" in first
    assert first != second


def test_step_zero_approval_uses_approval_header():
    q = "x"
    t = web_search_engine_step_chat_text(q, 0, approval_required=True)
    assert "approval" in t.lower() or "Approval" in t


def test_step_index_negative_treated_as_first():
    q = "q"
    assert web_search_engine_step_chat_text(q, -1, approval_required=False) == web_search_engine_step_chat_text(
        q, 0, approval_required=False
    )


def test_norm_research_query_collapses_whitespace_and_case():
    assert _norm_research_query("  Best  Pizza \n") == _norm_research_query("best pizza")
    assert _norm_research_query("") == ""


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
# Web research step budget tests (from test_web_research_step_budget.py)
# =============================================================================


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
