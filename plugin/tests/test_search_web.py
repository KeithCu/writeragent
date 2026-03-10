#!/usr/bin/env python
# coding: utf-8

"""
Standalone CLI harness to exercise the vendored smolagents-based web search agent.

This does NOT require LibreOffice or LocalWriter's UNO context. It talks directly
to OpenRouter using an OpenAI-compatible HTTP API, and uses the same
ToolCallingAgent + DuckDuckGo + VisitWebpage tool stack as LocalWriter's
`search_web` tool.

Usage:

  export OPENROUTER_API_KEY="sk-or-..."
  python -m scripts.test_search_web "What is the latest stable Python release and when was it released?"

You can pass a custom --max-tokens if desired, and override the model with --model.
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import requests


# Default OpenRouter model for the search sub-agent CLI.
DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3-nano-30b-a3b"


def _add_project_root_to_path() -> None:
    """Ensure the project root is on sys.path when run via `python -m`."""
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


_add_project_root_to_path()

from plugin.contrib.smolagents.models import (  # type: ignore  # noqa: E402
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    Model,
    TokenUsage,
)
from plugin.contrib.smolagents.utils import AgentParsingError  # type: ignore  # noqa: E402
from plugin.contrib.smolagents.agents import ToolCallingAgent  # type: ignore  # noqa: E402
from plugin.contrib.smolagents.default_tools import (  # type: ignore  # noqa: E402
    DuckDuckGoSearchTool,
    VisitWebpageTool,
)


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
                # toolcalling_agent.json uses text-only content; flatten it.
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

