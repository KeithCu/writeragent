# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Smolagents Model that forwards LLM calls to the LibreOffice host."""

from __future__ import annotations

from typing import Any

from plugin.contrib.smolagents.models import ChatMessage, Model, TokenUsage, remove_content_after_stop_sequences
from plugin.ppt_master.venv.ipc import rpc_llm


class HostRpcModel(Model):
    """Venv-side model: HTTP/auth on host via llm_request IPC."""

    def __init__(self, *, model_id: str | None, max_tokens: int, status_callback: object | None = None) -> None:
        super().__init__()
        self.model_id = model_id
        self.max_tokens = max_tokens
        self._status_callback = status_callback

    def generate(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ):
        del response_format, kwargs
        if self._status_callback:
            try:
                self._status_callback("Thinking...")
            except Exception:
                pass

        msg_dicts: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, ChatMessage):
                msg_dicts.append(m.model_dump())
            elif isinstance(m, dict):
                msg_dicts.append(m)
            else:
                msg_dicts.append({"role": "user", "content": str(m)})

        tools = None
        if tools_to_call_from:
            tools = []
            for t in tools_to_call_from:
                schema = getattr(t, "to_tool_calling_prompt_schema", None)
                if callable(schema):
                    tools.append(schema())
                elif hasattr(t, "name"):
                    tools.append({"type": "function", "function": {"name": t.name, "description": getattr(t, "description", "")}})

        result = rpc_llm(
            messages=msg_dicts,
            tools=tools,
            model=self.model_id,
            max_tokens=self.max_tokens,
        )
        content = result.get("content") or ""
        if stop_sequences is not None:
            trimmed = remove_content_after_stop_sequences(content, stop_sequences)
            if trimmed is not None:
                content = trimmed

        usage = result.get("usage") or {}
        token_usage = (
            TokenUsage(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0))
            if usage
            else None
        )
        return ChatMessage.from_dict(
            {
                "role": result.get("role", "assistant"),
                "content": content,
                "tool_calls": result.get("tool_calls"),
            },
            raw=result,
            token_usage=token_usage,
        )
